# InterstellarInformant 🚀

A Telegram bot that delivers **daily** and **weekly** astronomy updates for your location — including planets, Moon phases, ISS passes, and special celestial events.
Supports **Gemini 2.0 Flash** LLM mode for compact, well-structured bulletins.

---

## ✨ Features

* `/today` — Instant astronomy report for your saved location.
* `/weekly` — Concise 7-day outlook.
* Daily push updates at your preferred local time (via Telegram JobQueue).
* Data sources:

  * [timeanddate.com](https://www.timeanddate.com/astronomy/night/)
  * [EarthSky](https://earthsky.org/astronomy-essentials/visible-planets-tonight-mars-jupiter-venus-saturn-mercury/)
  * [Heavens-Above](https://heavens-above.com/)
* Optional LLM output via **Gemini 2.0 Flash** for cleaner formatting.
* Works with **city names** or **GPS coordinates**.

---

## 📦 Setup

### Requirements

* Python 3.10+
* Docker (optional, for containerized deployment)
* Telegram bot token

### Install

```bash
git clone https://github.com/yourusername/InterstellarInformant.git
cd InterstellarInformant
pip install -r requirements.txt
```

---

## ⚙️ Environment Variables

| Variable           | Description                                    |
| ------------------ | ---------------------------------------------- |
| `TELEGRAM_TOKEN`   | Your Telegram bot token                        |
| `GEMINI_API_KEY`   | Gemini API key (optional)                      |
| `GEMINI_MODEL`     | Defaults to `gemini-2.0-flash`                 |
| `ASTRO_USE_GEMINI` | `1` to enable Gemini, `0` to disable           |
| `ASTRO_TZ`         | Default timezone (e.g., `America/Detroit`)     |
| `ASTRO_TAD_PATH`   | Default timeanddate path (e.g., `usa/detroit`) |
| `ASTRO_DAILY_HOUR` | Default push hour (local)                      |
| `ASTRO_DAILY_MIN`  | Default push minute (local)                    |

---

## ▶️ Run Locally

```bash
python app/bot.py
```

---

## 🐳 Run with Docker

```bash
docker compose up --build -d
```

---

## 📌 Commands

* `/start` — Start bot & schedule daily updates
* `/today` — Today’s report
* `/weekly` — Weekly outlook
* `/setlocation <path>` — Change location (or share GPS)
* `/settime HH:MM` — Set daily push time
* `/settz <Area/City>` — Set timezone

---

## 📝 Notes

* If Gemini mode is enabled and a data source fails, bot falls back to LLM output automatically.
* Night time in reports uses the source website’s format (may be 12h or 24h depending on location).

---
## Updates:
* Review and make concise
* Extract more content for API/LLM summary
* Format response to include useful hyperlinks and so on.