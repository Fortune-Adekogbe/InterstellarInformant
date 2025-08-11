# app/bot.py
import datetime as dt
import logging
import os
import re
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters
)

from .store import (
    get_user, upsert_user, get_all_users, set_use_llm
)
from .event_sources import (
    fetch_timeanddate, fetch_iss, fetch_earthsky_summary, build_source_urls
)
from .llm_mode import gemini_render_today, gemini_render_weekly

DEFAULT_TZ = os.getenv("ASTRO_TZ", "America/Detroit")
DEFAULT_TAD_PATH = os.getenv("ASTRO_TAD_PATH", "usa/detroit")
DEFAULT_DAILY_HOUR = int(os.getenv("ASTRO_DAILY_HOUR", "17"))
DEFAULT_DAILY_MIN = int(os.getenv("ASTRO_DAILY_MIN", "0"))
ENV_USE_GEMINI = os.getenv("ASTRO_USE_GEMINI", "0") == "1"
ENV_DEBUG = os.getenv("ASTRO_DEBUG", "0") == "1"
LAST_BACKEND = {}  # user_id -> "LLM" | "API" | "LLM-FAIL"

# ---------- Text safety (no emojis / surrogates) ----------

def _safe_text(s: str) -> str:
    if s is None:
        return ""
    # Normalize away any surrogates (root cause of the old crashes)
    try:
        s = s.encode("utf-16", "surrogatepass").decode("utf-16", "ignore")
    except Exception:
        pass
    # Drop zero-width chars and control chars (keep \n/\t)
    s = s.replace("\u200b", "")
    s = "".join(ch for ch in s if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    # Tidy whitespace
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

async def _send_safe(update: Update, text: str) -> None:
    await update.message.reply_text(_safe_text(text))

async def _send_safe_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    await context.bot.send_message(chat_id=chat_id, text=_safe_text(text))


def _parse_time_local(tzname: str, hhmm: str) -> Optional[dt.datetime]:
    """
    Parse a local time string from timeanddate / Heavens-Above (e.g., '7:12 pm', '07:12 PM', '21:33', '21:33:10').
    Returns a timezone-aware datetime on *today* in the given tz.
    """
    if not hhmm:
        return None
    s = hhmm.strip().lower()
    s = s.replace("\u2013", "-").replace("\u2014", "-")  # normalize dashes
    tz = ZoneInfo(tzname)
    today = dt.datetime.now(tz).date()
    fmts = ["%I:%M %p", "%I:%M%p", "%H:%M", "%H:%M:%S"]
    for fmt in fmts:
        try:
            t = dt.datetime.strptime(s, fmt).time()
            return dt.datetime.combine(today, t, tzinfo=tz)
        except ValueError:
            continue
    return None

def _hmm(delta: dt.timedelta) -> str:
    mins = int(round(delta.total_seconds() / 60.0))
    if mins < 0:
        mins = 0
    h, m = divmod(mins, 60)
    return f"{h}h {m}m" if h else f"{m}m"

# ---------- Formatting ----------

def _format_today(summary: Dict, iss: Optional[Dict], earthsky: Optional[str]) -> str:
    L = []
    L.append(f"TODAY — {summary['city']} · {summary['date']}")
    if summary.get("sunset") or summary.get("sunrise"):
        L.append(f"Sunset {summary.get('sunset') or '?'} · Sunrise {summary.get('sunrise') or '?'}")
    if summary.get("moon_phase"):
        L.append(f"Moon: {summary['moon_phase']}")
    if summary.get("planets"):
        lines = []
        for p in summary["planets"]:
            if p["name"] not in {"Mercury", "Venus", "Mars", "Jupiter", "Saturn"}:
                continue
            bits = []
            if p.get("rise"):
                bits.append(f"↑ {p['rise']}")
            if p.get("set"):
                bits.append(f"↓ {p['set']}")
            if p.get("comment"):
                bits.append(p["comment"])
            line = f"- {p['name']}" + (": " + ", ".join(bits) if bits else "")
            lines.append(line)
        if lines:
            L.append("Planets:")
            L.extend(lines)
    if iss:
        L.append(f"ISS: start {iss['start']}, max {iss['max_alt']} at {iss['max_time']} (mag {iss['mag']})")
    if earthsky:
        L.append(f"EarthSky: {earthsky}")
    L.append("Sources: timeanddate.com · Heavens-Above · EarthSky")
    return "\n".join(L)

def _format_weekly(summary: Dict, iss: Optional[Dict], start_str: str, earthsky: Optional[str]) -> str:
    L = []
    L.append(f"WEEKLY OUTLOOK — {summary['city']} · starting {start_str}")
    bright = {p["name"]: p for p in summary.get("planets", []) if p["name"] in {"Mercury","Venus","Mars","Jupiter","Saturn"}}
    order = ["Venus", "Jupiter", "Saturn", "Mars", "Mercury"]
    for name in order:
        if name not in bright:
            continue
        if name in {"Venus","Jupiter"}:
            window = "pre-dawn best"
        elif name == "Saturn":
            window = "late night -> dawn"
        elif name == "Mars":
            window = "after dusk"
        else:
            window = "near twilight — hard"
        L.append(f"- {name}: {window}")
    if iss:
        L.append(f"- ISS: good pass {iss['date']} around {iss['max_time']} (max {iss['max_alt']})")
    if earthsky:
        L.append(f"EarthSky: {earthsky}")
    L.append("(For precise nightly times, use /today.)")
    L.append("Sources: timeanddate.com · Heavens-Above · EarthSky")
    return "\n".join(L)

def _format_now(summary: Dict, tzname: str, iss: Optional[Dict]) -> str:
    """
    Show what's happening in the next ~3 hours:
      - For each bright planet: whether it's up now (and when it sets), or rises soon
      - ISS pass only if today and within the 3h window (if we have lat/lon data)
    """
    now = dt.datetime.now(ZoneInfo(tzname))
    horizon = now + dt.timedelta(hours=3)

    lines = []
    lines.append(f"NOW — {summary['city']} · {now.strftime('%b %d, %Y %I:%M %p').lstrip('0')}")

    # Planet windows
    bright = [p for p in summary.get("planets", []) if p["name"] in {"Mercury","Venus","Mars","Jupiter","Saturn"}]
    planet_lines = []
    for p in bright:
        r = _parse_time_local(tzname, p.get("rise") or "")
        s = _parse_time_local(tzname, p.get("set") or "")
        if not r or not s:
            continue
        # handle spans over midnight
        if s <= r:
            s = s + dt.timedelta(days=1)
        if r <= now <= s:
            # currently up
            planet_lines.append(f"- {p['name']}: up now, sets in {_hmm(s - now)}")
        elif now < r <= horizon:
            planet_lines.append(f"- {p['name']}: rises in {_hmm(r - now)}")
        elif now < s <= horizon and r < now:
            planet_lines.append(f"- {p['name']}: sets in {_hmm(s - now)}")

    if planet_lines:
        lines.append("Planets (next 3h):")
        lines.extend(planet_lines)

    # ISS within 3h (best pass)
    if iss and iss.get("date") and iss.get("max_time"):
        max_dt = _parse_time_local(tzname, iss["max_time"])
        # Heavens-Above "date" is textual; assume "today" unless it's clearly different
        if max_dt and now <= max_dt <= horizon:
            lines.append(f"ISS: max at {max_dt.strftime('%I:%M %p').lstrip('0')} (max {iss['max_alt']})")

    # Night Time window from summary, if present
    if summary.get("night_time"):
        lines.append(f"Night window: {summary['night_time']}")

    if len(lines) == 1:
        lines.append("No obvious activity within ~3 hours.")
    lines.append("Tip: use /today for full details.")
    return "\n".join(lines)


def _summary_to_dict(summary_obj) -> Dict:
    return {
        "date": summary_obj.date,
        "city": summary_obj.city,
        "moon_phase": summary_obj.moon_phase,
        "night_time": summary_obj.night_time,
        "sunset": summary_obj.sunset,
        "sunrise": summary_obj.sunrise,
        "planets": [
            {"name": p.name, "rise": p.rise, "set": p.set, "comment": p.comment}
            for p in summary_obj.planets
        ],
    }

def _iss_to_dict(iss_obj) -> Optional[Dict]:
    if not iss_obj:
        return None
    return {
        "date": iss_obj.date, "start": iss_obj.start, "max_alt": iss_obj.max_alt,
        "max_time": iss_obj.max_time, "mag": iss_obj.mag
    }

# ---------- Scheduling ----------

async def _schedule_daily(context: ContextTypes.DEFAULT_TYPE, row: Dict) -> None:
    tz = row["tz"]
    hh = int(row["daily_hour"])
    mm = int(row["daily_minute"])
    jid = f"daily-{row['user_id']}"
    for j in context.job_queue.get_jobs_by_name(jid):
        j.schedule_removal()
    run_time = dt.time(hour=hh, minute=mm, tzinfo=ZoneInfo(tz))
    context.job_queue.run_daily(
        callback=push_daily,
        time=run_time,
        name=jid,
        data={"user_id": row["user_id"]}
    )

# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    row = get_user(uid)
    await _schedule_daily(context, row)
    kb = [[KeyboardButton("Share location", request_location=True)]]
    await _send_safe(update,
        "Astronomy Daily Bot ready.\n\n"
        "Commands:\n"
        "/today — instant report\n"
        "/weekly — 7-day outlook\n"
        "/setlocation  <timeanddate path>  or share GPS\n"
        "/settime HH:MM — daily push time\n"
        "/settz Area/City — IANA timezone\n"
        "/mode api|llm — toggle Gemini formatting\n"
        "/now — next ~3 hours\n"
        "/source — links to the sources\n"
        "/diag — show mode, Gemini config/probe, last backend used\n"
        f"Current: {row['tad_path']} @ {row['daily_hour']:02d}:{row['daily_minute']:02d} ({row['tz']})"
    )
    await update.message.reply_text(
        "Share GPS to enable ISS passes.", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True)
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_safe(update,
        "Commands:\n"
        "/today — instant report\n"
        "/weekly — next 7 days\n"
        "/setlocation <path> or share GPS\n"
        "/settime HH:MM — daily push time\n"
        "/settz Area/City — set timezone\n"
        "/mode api|llm — toggle Gemini formatter\n"
        "/now — next ~3 hours\n"
        "/source — links to the sources\n"
        "/diag — show mode, Gemini config/probe, last backend used"
    )

async def setlocation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    row = get_user(uid)
    if update.message.location:
        lat = update.message.location.latitude
        lon = update.message.location.longitude
        upsert_user(uid, tz=row["tz"], tad_path=row["tad_path"], lat=lat, lon=lon,
                    daily_hour=row["daily_hour"], daily_minute=row["daily_minute"],
                    use_llm=row["use_llm"])
        await _send_safe(update, f"Location saved (lat={lat:.4f}, lon={lon:.4f}).")
        return
    if not context.args:
        await _send_safe(update, "Send /setlocation <timeanddate path> (e.g., usa/detroit) or share GPS.")
        return
    path = context.args[0].strip().lstrip("/")
    upsert_user(uid, tz=row["tz"], tad_path=path, lat=row["lat"], lon=row["lon"],
                daily_hour=row["daily_hour"], daily_minute=row["daily_minute"],
                use_llm=row["use_llm"])
    await _send_safe(update, f"timeanddate page set to: {path}")

async def settime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    row = get_user(uid)
    if not context.args:
        await _send_safe(update, "Usage: /settime HH:MM (24h)")
        return
    m = re.match(r"^(\d{1,2}):(\d{2})$", context.args[0])
    if not m:
        await _send_safe(update, "Bad time. Use HH:MM, e.g., 15:00")
        return
    h, mi = int(m.group(1)), int(m.group(2))
    upsert_user(uid, tz=row["tz"], tad_path=row["tad_path"], lat=row["lat"], lon=row["lon"],
                daily_hour=h, daily_minute=mi, use_llm=row["use_llm"])
    await _schedule_daily(context, get_user(uid))
    await _send_safe(update, f"Daily push set to {h:02d}:{mi:02d} ({row['tz']}).")

async def settz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    row = get_user(uid)
    if not context.args:
        await _send_safe(update, "Usage: /settz Area/City, e.g., America/Detroit")
        return
    tzname = context.args[0]
    try:
        _ = ZoneInfo(tzname)
    except Exception:
        await _send_safe(update, "Unknown TZ. See IANA tz database.")
        return
    upsert_user(uid, tz=tzname, tad_path=row["tad_path"], lat=row["lat"], lon=row["lon"],
                daily_hour=row["daily_hour"], daily_minute=row["daily_minute"],
                use_llm=row["use_llm"])
    await _schedule_daily(context, get_user(uid))
    await _send_safe(update, f"Timezone set to {tzname}.")

async def mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    row = get_user(uid)
    if not context.args:
        await _send_safe(update, f"Current mode: {'LLM' if row['use_llm'] else 'API'}.\nUse /mode api or /mode llm.")
        return
    val = context.args[0].lower()
    if val not in {"api", "llm"}:
        await _send_safe(update, "Usage: /mode api|llm")
        return
    set_use_llm(uid, val == "llm")
    await _send_safe(update, f"Mode set to {'LLM' if val=='llm' else 'API'}.")

async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.chat.send_action(ChatAction.TYPING)
    uid = update.effective_user.id
    row = get_user(uid)

    # Fetch data
    summary_obj = fetch_timeanddate(row["tad_path"], row["tz"])
    iss_obj = None
    if row.get("lat") is not None and row.get("lon") is not None:
        try:
            iss_obj = fetch_iss(row["lat"], row["lon"], row["tz"])
        except Exception:
            iss_obj = None
    earthsky = fetch_earthsky_summary()

    summary = _summary_to_dict(summary_obj)
    iss = _iss_to_dict(iss_obj)

    # LLM formatting (per-user flag overrides env default)
    use_llm = bool(row["use_llm"] or ENV_USE_GEMINI)
    if use_llm:
        payload = {
            "city": summary["city"], "date": summary["date"],
            "sunset": summary["sunset"], "sunrise": summary["sunrise"],
            "moon_phase": summary["moon_phase"],
            "planets": summary["planets"],
            "iss": iss
        }
        txt = gemini_render_today(payload, row["lat"], row["lon"], row["tz"])
        if txt:
            LAST_BACKEND[uid] = "LLM"
            if ENV_DEBUG:
                txt = (txt + "\nRenderer: LLM")
            await _send_safe(update, txt)
            return
        # fall through if LLM failed
        LAST_BACKEND[uid] = "LLM-FAIL"
    text = _format_today(summary, iss, earthsky)

    LAST_BACKEND[uid] = "API"
    if ENV_DEBUG:
        text = (text + "\nRenderer: API")
    await _send_safe(update, text)

async def weekly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.chat.send_action(ChatAction.TYPING)
    uid = update.effective_user.id
    row = get_user(uid)
    summary_obj = fetch_timeanddate(row["tad_path"], row["tz"])
    iss_obj = None
    if row.get("lat") is not None and row.get("lon") is not None:
        try:
            iss_obj = fetch_iss(row["lat"], row["lon"], row["tz"])
        except Exception:
            iss_obj = None
    earthsky = fetch_earthsky_summary()

    summary = _summary_to_dict(summary_obj)
    iss = _iss_to_dict(iss_obj)
    start_str = dt.datetime.now(ZoneInfo(row["tz"])).strftime("%b %d, %Y")

    use_llm = bool(row["use_llm"] or ENV_USE_GEMINI)
    if use_llm:
        payload = {
            "city": summary["city"], "start": start_str,
            "planets": {p["name"]: {"rise": p["rise"], "set": p["set"], "note": p.get("comment")} for p in summary["planets"] if p["name"] in {"Mercury","Venus","Mars","Jupiter","Saturn"}},
            "iss": iss
        }
        txt = gemini_render_weekly(payload, row["lat"], row["lon"], row["tz"])
        if txt:
            LAST_BACKEND[uid] = "LLM"
            if ENV_DEBUG:
                txt = (txt + "\nRenderer: LLM")
            await _send_safe(update, txt)
            return
        # fall through if LLM failed
        LAST_BACKEND[uid] = "LLM-FAIL"

    text = _format_weekly(summary, iss, start_str, earthsky)
    LAST_BACKEND[uid] = "API"
    if ENV_DEBUG:
        text = (text + "\nRenderer: API")
    await _send_safe(update, text)

async def push_daily(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data or {}
    uid = data.get("user_id")
    if not uid:
        return
    row = get_user(uid)
    try:
        summary_obj = fetch_timeanddate(row["tad_path"], row["tz"])
        iss_obj = None
        if row.get("lat") is not None and row.get("lon") is not None:
            iss_obj = fetch_iss(row["lat"], row["lon"], row["tz"])
        earthsky = fetch_earthsky_summary()

        summary = _summary_to_dict(summary_obj)
        iss = _iss_to_dict(iss_obj)
        use_llm = bool(row["use_llm"] or ENV_USE_GEMINI)
        if use_llm:
            payload = {
                "city": summary["city"], "date": summary["date"],
                "sunset": summary["sunset"], "sunrise": summary["sunrise"],
                "moon_phase": summary["moon_phase"], "planets": summary["planets"], "iss": iss
            }
            txt = gemini_render_today(payload, row["lat"], row["lon"], row["tz"])
            if txt:
                LAST_BACKEND[uid] = "LLM"
                if ENV_DEBUG:
                    txt = (txt + "\nRenderer: LLM")
                await _send_safe_chat(context, uid, txt)
                return
            # fall through if LLM failed
            LAST_BACKEND[uid] = "LLM-FAIL"

        text = _format_today(summary, iss, earthsky)
        LAST_BACKEND[uid] = "API"
        if ENV_DEBUG:
            text = (text + "\nRenderer: API")
        await _send_safe_chat(context, uid, text)
    except Exception as e:
        logging.exception("daily push failed: %s", e)

async def _post_init(app: Application) -> None:
    # Recreate jobs for all users on startup
    from zoneinfo import ZoneInfo
    for row in get_all_users():
        tz = row["tz"]
        run_time = dt.time(hour=int(row["daily_hour"]), minute=int(row["daily_minute"]), tzinfo=ZoneInfo(tz))
        app.job_queue.run_daily(callback=push_daily, time=run_time, name=f"daily-{row['user_id']}", data={"user_id": row["user_id"]})

async def now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.chat.send_action(ChatAction.TYPING)
    uid = update.effective_user.id
    row = get_user(uid)
    summary_obj = fetch_timeanddate(row["tad_path"], row["tz"])
    iss_obj = None
    if row.get("lat") is not None and row.get("lon") is not None:
        try:
            iss_obj = fetch_iss(row["lat"], row["lon"], row["tz"])
        except Exception:
            iss_obj = None
    summary = _summary_to_dict(summary_obj)
    iss = _iss_to_dict(iss_obj)
    text = _format_now(summary, row["tz"], iss)
    await _send_safe(update, text)

async def source_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    row = get_user(uid)
    urls = build_source_urls(row["tad_path"], row["lat"], row["lon"], row["tz"])
    lines = ["SOURCES:"]
    lines.append(f"- timeanddate: {urls['timeanddate']}")
    lines.append(f"- EarthSky: {urls['earthsky']}")
    if urls.get("heavens_above"):
        lines.append(f"- Heavens-Above (ISS): {urls['heavens_above']}")
    else:
        lines.append("- Heavens-Above (ISS): share GPS with /setlocation to enable")
    await _send_safe(update, "\n".join(lines))

async def diag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    row = get_user(uid)
    mode_str = "LLM" if row["use_llm"] or (os.getenv("ASTRO_USE_GEMINI", "0") == "1") else "API"

    # quick live probe (does not send the formatted result)
    payload = {"city":"Probe", "date":"N/A", "sunset":None, "sunrise":None, "moon_phase":None, "planets":[], "iss":None}
    probe = gemini_render_today(payload, row["lat"], row["lon"], row["tz"])
    gem_cfg = "configured" if os.getenv("GEMINI_API_KEY") else "missing-key"
    gem_ok = "OK" if probe else "FAIL"

    last = LAST_BACKEND.get(uid, "unknown")
    lines = [
        f"Mode: {mode_str}",
        f"Gemini: {gem_cfg} / probe {gem_ok}",
        f"Last backend used: {last}",
        f"Debug footer: {'on' if ENV_DEBUG else 'off'}"
    ]
    await _send_safe(update, "\n".join(lines))



def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.getenv("TELEGRAM_TOKEN", "")
    if not token:
        raise SystemExit("TELEGRAM_TOKEN not set")
    app = Application.builder().token(token).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("weekly", weekly))
    app.add_handler(CommandHandler("setlocation", setlocation))
    app.add_handler(CommandHandler("settime", settime))
    app.add_handler(CommandHandler("settz", settz))
    app.add_handler(CommandHandler("mode", mode))
    app.add_handler(CommandHandler("now", now_cmd))
    app.add_handler(CommandHandler("source", source_cmd))
    app.add_handler(CommandHandler("diag", diag))


    app.add_handler(MessageHandler(filters.LOCATION, setlocation))

    # Basic error handler
    async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
        logging.exception("PTB error: %s", context.error)
    app.add_error_handler(on_error)

    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()