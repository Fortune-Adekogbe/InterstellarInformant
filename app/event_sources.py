# app/event_sources.py
import datetime as dt
import re
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

HTTP_TIMEOUT = 12
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
}

TAD_BASE = "https://www.timeanddate.com/astronomy/night/"
EARTHSKY_URL = (
    "https://earthsky.org/astronomy-essentials/"
    "visible-planets-tonight-mars-jupiter-venus-saturn-mercury/"
)
HA_BASE = "https://heavens-above.com/PassSummary.aspx"

@dataclass
class PlanetWindow:
    name: str
    rise: Optional[str] = None
    set: Optional[str] = None
    comment: Optional[str] = None

@dataclass
class NightSummary:
    date: str
    city: str
    moon_phase: Optional[str]
    night_time: Optional[str]
    sunset: Optional[str]
    sunrise: Optional[str]
    planets: List[PlanetWindow]

@dataclass
class ISSPass:
    date: str
    start: str
    max_alt: str
    max_time: str
    mag: str

def _now_in_tz(tzname: str) -> dt.datetime:
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz = ZoneInfo("UTC")
    return dt.datetime.now(tz)

def fetch_timeanddate(tad_path: str, default_tz: str) -> NightSummary:
    """
    Fetch & parse the timeanddate Night Sky page for a location path, e.g. 'usa/detroit'.
    Parsing is defensive: if their layout changes, we still return a minimal summary.
    """
    url = TAD_BASE + tad_path.lstrip("/")
    r = requests.get(url, headers=UA, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    # City title
    city = tad_path
    h1 = soup.find("h1")
    if h1:
        txt = h1.get_text(" ", strip=True)
        city = txt.replace("Night Sky Tonight in ", "").strip() or tad_path

    moon_phase = None
    night_time = None
    sunset = None
    sunrise = None
    planets: List[PlanetWindow] = []

    # Extract "Night Time", "Sunset", "Sunrise", and a textual Moon percentage if present.
    info = soup.find(string=re.compile(r"Night Time:", re.I))
    if info:
        block = info.parent
        text = block.get_text(" ", strip=True)
        m = re.search(r"Moon:\s*([0-9.]+%)", text)
        if m:
            moon_phase = m.group(1)
        nt = re.search(r"Night Time:\s*([^S]+)\s*Sunset:\s*([^E]+?)\s*.*?Sunrise:\s*([^\n]+)", text)
        if nt:
            night_time = nt.group(1).strip()
            sunset = nt.group(2).strip()
            sunrise = nt.group(3).strip()

    # Planets table (Planets Visible in …)
    plan_hdr = soup.find(string=re.compile(r"Planets Visible in", re.I))
    if plan_hdr:
        table = plan_hdr.find_parent().find_next("table")
        if table:
            for row in table.select("tbody tr"):
                cols = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
                if len(cols) >= 5:
                    name, rise, set_, comment = cols[0], cols[1], cols[2], cols[4]
                    planets.append(PlanetWindow(name=name, rise=rise, set=set_, comment=comment))

    # If no table, try per-planet sections as fallback
    if not planets:
        for p in ["Mercury", "Venus", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune"]:
            h3 = soup.find("h3", string=re.compile(fr"^{p} rise and set", re.I))
            if not h3:
                continue
            block = h3.find_parent()
            txt = block.get_text(" ", strip=True)
            times = re.findall(r"\b(\d{1,2}:\d{2}\s*[ap]m)\b", txt, flags=re.I)
            rise = times[0] if times else None
            set_ = times[1] if len(times) > 1 else None
            comment = None
            cm = re.search(r"(Good|Fairly good|Average|Difficult|Perfect|Very difficult).*?visibility",
                           txt, flags=re.I)
            if cm:
                comment = cm.group(0)
            planets.append(PlanetWindow(name=p, rise=rise, set=set_, comment=comment))

    # Date line: if missing, use current date in default tz
    date_str = _now_in_tz(default_tz).strftime("%b %d, %Y")

    return NightSummary(
        date=date_str, city=city, moon_phase=moon_phase, night_time=night_time,
        sunset=sunset, sunrise=sunrise, planets=planets
    )

def fetch_iss(lat: float, lon: float, tzname: str) -> Optional[ISSPass]:
    tzabbr = dt.datetime.now(ZoneInfo(tzname)).tzname() or "UTC"
    params = {
        "satid": "25544",
        "lat": f"{lat:.4f}",
        "lng": f"{lon:.4f}",
        "alt": "0",
        "loc": "Observer",
        "tz": tzabbr,
    }
    r = requests.get(HA_BASE, params=params, headers=UA, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    table = soup.find("table")
    if not table:
        return None

    best: Optional[ISSPass] = None
    for tr in table.select("tr"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(tds) >= 10:
            date = tds[0]
            mag = tds[1]
            start_time = tds[2]
            max_time = tds[5]
            max_alt = tds[6]
            try:
                alt_deg = int(re.sub(r"[^0-9]", "", max_alt))
                mag_val = float(mag)
            except Exception:
                alt_deg = 0
                mag_val = 99.0
            score = alt_deg - mag_val * 5  # higher better
            if not best:
                best = ISSPass(date=date, start=start_time, max_alt=max_alt, max_time=max_time, mag=mag)
            else:
                prev_alt = int(re.sub(r"[^0-9]", "", best.max_alt))
                prev_mag = float(best.mag)
                if score > (prev_alt - prev_mag * 5):
                    best = ISSPass(date=date, start=start_time, max_alt=max_alt, max_time=max_time, mag=mag)
    return best

def fetch_earthsky_summary() -> Optional[str]:
    try:
        r = requests.get(EARTHSKY_URL, headers=UA, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        h = soup.find(["h1", "h2"], string=re.compile("Visible planets", re.I))
        p = h.find_next("p") if h else soup.find("p")
        if not p:
            return None
        txt = p.get_text(" ", strip=True)
        return (txt[:400] + "…") if len(txt) > 400 else txt
    except Exception:
        return None

def build_source_urls(tad_path: str, lat: Optional[float], lon: Optional[float], tzname: str) -> dict:
    """
    Return direct links for the data we use:
      - timeanddate Night Sky page for the user's chosen path
      - EarthSky visible planets page
      - Heavens-Above ISS pass summary for user's lat/lon (if available)
    """
    td_url = (TAD_BASE + tad_path.lstrip("/"))
    es_url = EARTHSKY_URL

    ha_url = None
    if lat is not None and lon is not None:
        try:
            tzabbr = dt.datetime.now(ZoneInfo(tzname)).tzname() or "UTC"
        except Exception:
            tzabbr = "UTC"
        params = {
            "satid": "25544",
            "lat": f"{lat:.4f}",
            "lng": f"{lon:.4f}",
            "alt": "0",
            "loc": "Observer",
            "tz": tzabbr,
        }
        ha_url = f"{HA_BASE}?{urlencode(params)}"

    return {"timeanddate": td_url, "earthsky": es_url, "heavens_above": ha_url}
