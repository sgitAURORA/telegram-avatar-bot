"""Меняет аву и описание в Telegram по фазе солнца.

python tg_avatar.py        - обновить профиль, если фаза сменилась
python tg_avatar.py plan   - пересчитать расписание (cron) в .github/workflows/avatar.yml
"""
import asyncio
import datetime as dt
import json
import os
import random
import re
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import sunrise, sunset

# --- настройки -------------------------------------------------------------
LAT, LON = 43.06, 74.48          # район Манаса, Бишкек
TZ = ZoneInfo("Asia/Bishkek")
PHOTOS = Path("photos")
STATE = Path("state.json")
WORKFLOW = Path(".github/workflows/avatar.yml")
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
RECENT_KEEP = 50                 # сколько последних фото не повторять

# Лимит описания: 70 символов (140 с Premium)
BIO = {
    "golden_morning": "🌅 утренний золотой час",
    "blue_morning": "🌌 синий час перед рассветом",
    "day": "☀️ день",
    "golden_evening": "🌇 вечерний золотой час",
    "blue_evening": "🌆 синий час после заката",
    "night": "🌙 ночь",
}

BLUE = dt.timedelta(hours=1)     # синий: час ДО восхода и час ПОСЛЕ заката
GOLDEN = dt.timedelta(hours=1)   # золотой: час ПОСЛЕ восхода и час ДО заката
# ---------------------------------------------------------------------------


def sun_times(date: dt.date) -> tuple[dt.datetime, dt.datetime]:
    obs = Observer(latitude=LAT, longitude=LON)
    return sunrise(obs, date, TZ), sunset(obs, date, TZ)


def get_phase(now: dt.datetime) -> str:
    sr, ss = sun_times(now.date())
    if sr - BLUE <= now < sr:
        return "blue_morning"
    if sr <= now < sr + GOLDEN:
        return "golden_morning"
    if sr + GOLDEN <= now < ss - GOLDEN:
        return "day"
    if ss - GOLDEN <= now < ss:
        return "golden_evening"
    if ss <= now < ss + BLUE:
        return "blue_evening"
    return "night"


def pick_photo(phase: str, recent: list[str]) -> Path | None:
    """Случайное фото из photos/<phase>/, не повторяя недавние."""
    files = [p for p in (PHOTOS / phase).glob("*") if p.suffix.lower() in IMG_EXT]
    if not files:
        return None
    mine = [r for r in recent if r.startswith(phase + "/")]
    if len(files) > 1:
        mine = mine[-min(RECENT_KEEP, len(files) - 1):]
    else:
        mine = []
    fresh = [p for p in files if f"{phase}/{p.name}" not in mine]
    return random.choice(fresh or files)


def prepare_photo(src: Path) -> Path:
    """Квадратная обрезка по центру, до 1280px, JPEG — Telegram такое всегда берёт."""
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    w, h = img.size
    s = min(w, h)
    left, top = (w - s) // 2, (h - s) // 2
    img = img.crop((left, top, left + s, top + s))
    if s > 1280:
        img = img.resize((1280, 1280), Image.Resampling.LANCZOS)
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    img.save(tmp.name, "JPEG", quality=92)
    return Path(tmp.name)


async def run() -> None:
    from telethon import TelegramClient, errors, functions, utils
    from telethon.sessions import StringSession

    now = dt.datetime.now(TZ)
    phase = get_phase(now)
    key = f"day-{now.hour}" if phase == "day" else phase
    force = os.environ.get("FORCE") == "1"

    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    print(f"{now:%Y-%m-%d %H:%M} phase={phase} key={key} last={state.get('key')}")
    if state.get("key") == key and not force:
        print("ничего менять не надо")
        return

    photo = pick_photo(phase, state.get("recent", []))
    if photo is None:
        print(f"нет фото в photos/{phase}/ — меняю только описание")

    try:
        async with TelegramClient(
            StringSession(os.environ["TG_SESSION"]),
            int(os.environ["TG_API_ID"]),
            os.environ["TG_API_HASH"],
        ) as client:
            if photo:
                ready = prepare_photo(photo)
                up = await client.upload_file(str(ready))
                res = await client(functions.photos.UploadProfilePhotoRequest(file=up))
                prev_id = state.get("photo_id")
                state["photo_id"] = res.photo.id
                state["recent"] = (state.get("recent", []) + [f"{phase}/{photo.name}"])[-300:]
                # удаляем только ту аву, которую залили сами раньше
                if prev_id:
                    photos = await client.get_profile_photos("me", limit=10)
                    old = [p for p in photos if p.id == prev_id]
                    if old:
                        await client(functions.photos.DeletePhotosRequest(
                            id=[utils.get_input_photo(p) for p in old]
                        ))
            try:
                await client(functions.account.UpdateProfileRequest(about=BIO[phase]))
            except Exception as ex:  # например, описание не изменилось
                print("bio:", ex)
    except errors.FloodWaitError as ex:
        print(f"Telegram просит подождать {ex.seconds} c — повторю в следующий запуск")
        return

    state["key"] = key
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    print("готово")


def plan() -> None:
    """Переписывает cron в avatar.yml под сегодняшний день (по Бишкеку)."""
    today = dt.datetime.now(TZ).date()
    sr, ss = sun_times(today)
    utc = dt.timezone.utc

    lines = ['    - cron: "13 */3 * * *"   # страховка, раз в 3 часа']

    hours = sorted({
        t.astimezone(utc).hour
        for t in (dt.datetime.combine(today, dt.time(h, 3), TZ) for h in range(24))
        if sr + GOLDEN <= t < ss - GOLDEN
    })
    if hours:
        lines.append(f'    - cron: "3 {",".join(map(str, hours))} * * *"   # дневные фото')

    seen = set()
    for p in [sr - BLUE, sr, sr + GOLDEN, ss - GOLDEN, ss, ss + BLUE]:
        # +1 минута, чтобы запуск точно не оказался раньше границы
        u = (p + dt.timedelta(minutes=1)).astimezone(utc)
        cron = f"{u.minute} {u.hour} {u.day} {u.month} *"
        if cron not in seen:
            seen.add(cron)
            lines.append(f'    - cron: "{cron}"   # {p:%H:%M} по Бишкеку')

    block = "\n".join(lines)
    pattern = r"(# BEGIN-GENERATED\n).*?(\n[ \t]*# END-GENERATED)"
    text = WORKFLOW.read_text(encoding="utf-8")
    if not re.search(pattern, text, flags=re.S):
        print("маркеры BEGIN-GENERATED/END-GENERATED не найдены — расписание не трогаю")
        return
    new = re.sub(pattern, lambda m: m.group(1) + block + m.group(2), text, flags=re.S)
    WORKFLOW.write_text(new, encoding="utf-8")
    print(block)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "plan":
        plan()
    else:
        asyncio.run(run())
