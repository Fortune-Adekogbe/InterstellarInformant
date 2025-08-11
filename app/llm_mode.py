# app/llm_mode.py
import os, datetime as dt
from typing import Dict, Optional, List
import requests
from bs4 import BeautifulSoup

# Optional dependency
try:
    from google import genai
except Exception:
    genai = None

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "models/gemini-2.0-flash-001")
SEARCH_DOMAINS = "" #"site:in-the-sky.org OR site:earthsky.org OR site:timeanddate.com OR site:heavens-above.com"
FETCH_RESULT_PAGES = os.getenv("ASTRO_LLM_FETCH_PAGES", "1") == "1"

# Simple ASCII sanitizer to avoid emoji/surrogates
def _safe_text(s: str) -> str:
    s = s.encode("utf-8", "ignore").decode("utf-8", "ignore")
    # strip non-ASCII aggressively for safety in Telegram/plain
    return "".join(ch if ord(ch) < 128 else " " for ch in s).replace("\u200b", "").strip()


client = genai.Client(api_key=GEMINI_API_KEY)

def _search_serpapi(query: str, blob_limit=3) -> List[Dict]:
    key = os.getenv("SERPAPI_API_KEY")
    if not key:
        return []
    url = "https://serpapi.com/search.json"
    params = {"engine": "google", "q": query, "num": 10, "api_key": key}

    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()  # Raise an HTTPError for bad responses (4xx or 5xx)
        js = response.json()
    except requests.exceptions.RequestException as e:
        print(f"An error occurred: {e}")

    result = []
    
    for i, it in enumerate(js.get("organic_results", [])[:8]):
        res = {"title": it.get("title"), "link": it.get("link"), "snippet": it.get("snippet")}

        if FETCH_RESULT_PAGES and it.get("link") and i < blob_limit:
            txt = _fetch_page_text(it.get("link"))
            if txt:
                res["page_blob"] = txt
        result.append(res)
    return result

def _fetch_page_text(url: str, timeout: int = 12) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, timeout=timeout, headers=headers)
        html = response.text #errors="ignore")
    except Exception:
        print(f"Failed to open {url}.")
        return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ", strip=True).split())
    # keep it short for token budget
    return text#[:4000]

def gemini_render_today(payload: Dict, lat: float, lon: float, tzname: str) -> Optional[str]:
    d = dt.datetime.today()
    q = f"astronomy events today {str(d.date())} {SEARCH_DOMAINS}"
    serp = _search_serpapi(q)
    context = "\n".join([f"- {x['title']}: {x['snippet']} ({x['link']})" for x in serp])

    prompt = (
        f"You are an astronomy assistant. Using the search result and data below, list notable *observable* sky events for {str(d.date())} near lat {lat:.2f}, lon {lon:.2f} (timezone {tzname}). Prefer naked-eye events when possible. Output short bullet-like lines; include difficulty if obvious (naked, binoculars, small, four-inch, large). If uncertain, say so briefly."
        # "remember that you are formatting a concise astronomy bulletin for a Telegram bot. "
        "Constraints: no Markdown/HTML, use emojis if necessary, use bullet points with dashes, keep compact. " # ASCII only
        "Structure: First line 'TODAY — {city} · {date}'. Next line 'Sunset HH:MM · Sunrise HH:MM' if available. "
        "Then 'Moon: ' if available. Then 'Planets:' with up to 5 lines (Mercury, Venus, Mars, Jupiter, Saturn) "
        "like 'Name: ↑ rise, ↓ set, note'. If ISS exists add 'ISS: start , max  at  (mag )'. "
        "Then, you are free to add all relevant updates."
        "Finish with 'Sources: timeanddate.com · Heavens-Above · EarthSky'. "
        f"Data JSON: {payload}"
        f"Snippets: {context}"
    )

    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )
        text = getattr(resp, "text", "") or ""
        return _safe_text(text)
    except Exception:
        return None

def gemini_render_weekly(payload: Dict, lat: float, lon: float, tzname: str) -> Optional[str]:
    d = dt.datetime.today()
    q = f"astronomy events over the next 7 days (today is: {str(d.date())}) {SEARCH_DOMAINS}"
    serp = _search_serpapi(q)
    context = "\n".join([f"- {x['title']}: {x['snippet']} ({x['link']})" for x in serp])

    prompt = (
        f"You are an astronomy assistant. Using the search snippets and data below, list notable *observable* sky events for {d.isoformat()} near lat {lat:.2f}, lon {lon:.2f} (timezone {tzname}). Prefer naked-eye events when possible. Output short bullet-like lines; include difficulty if obvious (naked, binoculars, small, four-inch, large). If uncertain, say so briefly."
        "Format a compact 7-day outlook for a Telegram bot."# ASCII only. "
        "Constraints: no Markdown/HTML, use emojis if necessary, use bullet points with dashes, keep compact. "
        "First line: 'WEEKLY OUTLOOK — {city} · starting {start}'. "
        "Then 4–6 bullets summarizing visibility windows for Venus, Jupiter, Saturn, Mars, Mercury. "
        "If ISS and other satellite info exists, include one or more bullet. "
        "Then, you are free to add all relevant updates."
        "Close with '(For precise nightly times, use /today.)' and 'Sources: timeanddate.com · Heavens-Above · EarthSky'. "
        f"Data JSON: {payload}"
        f"Snippets: {context}"
    )
    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )
        text = getattr(resp, "text", "") or ""
        return _safe_text(text)
    except Exception:
        return None
