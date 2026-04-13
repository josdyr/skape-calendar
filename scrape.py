#!/usr/bin/env python3
"""Scrape skape.no/kurs/kurskalender, enrich new events via Claude, emit skape.ics."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from enrich import enrich_events

LIST_URL = "https://www.skape.no/kurs/kurskalender"
UA = "skape-calendar-bot (+https://github.com/josdyr/skape-calendar)"
CACHE_PATH = Path("data/enrichment.json")
DETAIL_TEXT_MAX = 3000  # chars of detail-page text to send to Claude per event

NO_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "mai": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "des": 12,
}

TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})")
DATE_RE = re.compile(r"(\d{1,2})\s*([a-zæøå]+)\.?\s*(\d{4})", re.IGNORECASE)

VTIMEZONE = [
    "BEGIN:VTIMEZONE",
    "TZID:Europe/Oslo",
    "X-LIC-LOCATION:Europe/Oslo",
    "BEGIN:STANDARD",
    "DTSTART:19701025T030000",
    "TZOFFSETFROM:+0200",
    "TZOFFSETTO:+0100",
    "TZNAME:CET",
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
    "END:STANDARD",
    "BEGIN:DAYLIGHT",
    "DTSTART:19700329T020000",
    "TZOFFSETFROM:+0100",
    "TZOFFSETTO:+0200",
    "TZNAME:CEST",
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
    "END:DAYLIGHT",
    "END:VTIMEZONE",
]


def parse_list_date(s: str):
    m = DATE_RE.search(s.strip().lower())
    if not m:
        return None
    month = NO_MONTHS.get(m.group(2)[:3])
    if not month:
        return None
    return int(m.group(3)), month, int(m.group(1))


def fetch_detail(session, url):
    """Return (time_window_or_none, cleaned_text_or_empty)."""
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
    except requests.RequestException:
        return None, ""
    tw = None
    m = TIME_RE.search(r.text)
    if m:
        h1, m1, h2, m2 = map(int, m.groups())
        tw = (h1, m1), (h2, m2)
    detail_soup = BeautifulSoup(r.text, "html.parser")
    for tag in detail_soup(["script", "style", "nav", "header", "footer", "svg"]):
        tag.decompose()
    text = detail_soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return tw, text[:DETAIL_TEXT_MAX]


def esc(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\n", r"\n")
    )


def fold(line: str) -> str:
    b = line.encode("utf-8")
    if len(b) <= 75:
        return line
    parts = []
    while len(b) > 75:
        cut = 75
        while cut > 0 and (b[cut] & 0xC0) == 0x80:
            cut -= 1
        parts.append(b[:cut].decode("utf-8"))
        b = b[cut:]
    parts.append(b.decode("utf-8"))
    return "\r\n ".join(parts)


def load_cache() -> dict[str, dict]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"WARN: {CACHE_PATH} is not valid JSON; starting fresh", file=sys.stderr)
    return {}


def save_cache(cache: dict[str, dict]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_description(url: str, meta: dict | None) -> str:
    if not meta:
        return f"Les mer: {url}"
    lines = []
    if meta.get("summary"):
        lines.append(meta["summary"])
        lines.append("")
    kv = [
        ("Søknadsfrist", meta.get("registration_deadline")),
        ("Pris", f"{int(meta['price_nok'])} NOK" if meta.get("price_nok") == 0 else
                 f"{meta['price_nok']} NOK" if meta.get("price_nok") is not None else None),
        ("Arrangør", meta.get("organizer")),
        ("Kursholder", meta.get("instructor")),
        ("Språk", meta.get("language")),
        ("Målgruppe", meta.get("audience")),
    ]
    if meta.get("price_nok") == 0:
        kv[1] = ("Pris", "Gratis")
    for label, value in kv:
        if value:
            lines.append(f"{label}: {value}")
    if meta.get("registration_url"):
        lines.append(f"Påmelding: {meta['registration_url']}")
    lines.append(f"Les mer: {url}")
    return "\n".join(lines)


def build_location(listing_loc: str, meta: dict | None) -> str:
    if meta:
        if meta.get("location_physical"):
            return meta["location_physical"]
        if meta.get("is_digital"):
            return "Digitalt (webinar)"
    return listing_loc


def main() -> int:
    session = requests.Session()
    session.headers["User-Agent"] = UA

    r = session.get(LIST_URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    cards = soup.select("div.calendar-link")
    if not cards:
        print("ERROR: no .calendar-link elements found", file=sys.stderr)
        return 1

    events = []
    for card in cards:
        a = card.find("a", href=True)
        title_el = card.select_one(".bitter")
        date_el = card.select_one(".time")
        loc_el = card.select_one(".location span")
        if not (a and title_el and date_el):
            continue
        date_str = date_el.get_text(" ", strip=True)
        ymd = parse_list_date(date_str)
        if not ymd:
            continue
        url = urljoin(LIST_URL, a["href"])
        uid = hashlib.sha1(url.encode("utf-8")).hexdigest() + "@skape-calendar"
        title = title_el.get_text(" ", strip=True)
        location = loc_el.get_text(" ", strip=True) if loc_el else ""
        tw, detail_text = fetch_detail(session, url)
        events.append({
            "uid": uid,
            "url": url,
            "title": title,
            "location": location,
            "date_str": date_str,
            "ymd": ymd,
            "tw": tw,
            "detail_text": detail_text,
        })

    if not events:
        print("ERROR: parsed 0 events — HTML structure may have changed", file=sys.stderr)
        return 1

    cache = load_cache()
    new_events = [e for e in events if e["uid"] not in cache]
    print(f"scrape: {len(events)} total, {len(new_events)} new, {len(events) - len(new_events)} cached", file=sys.stderr)

    if new_events:
        new_meta = enrich_events(new_events)
        for uid, meta in new_meta.items():
            cache[uid] = meta
        save_cache(cache)
    else:
        # touch nothing — keep file on disk identical
        pass

    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//josdyr/skape-calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Skape kurskalender",
        "X-WR-TIMEZONE:Europe/Oslo",
        "X-PUBLISHED-TTL:P1D",
        "REFRESH-INTERVAL;VALUE=DURATION:P1D",
        *VTIMEZONE,
    ]
    with_time = 0
    for ev in events:
        uid = ev["uid"]
        url = ev["url"]
        title = ev["title"]
        y, mo, d = ev["ymd"]
        tw = ev["tw"]
        meta = cache.get(uid)
        out.append("BEGIN:VEVENT")
        out.append(f"UID:{uid}")
        out.append(f"DTSTAMP:{now}")
        if tw:
            (sh, sm), (eh, em) = tw
            out.append(f"DTSTART;TZID=Europe/Oslo:{y:04d}{mo:02d}{d:02d}T{sh:02d}{sm:02d}00")
            out.append(f"DTEND;TZID=Europe/Oslo:{y:04d}{mo:02d}{d:02d}T{eh:02d}{em:02d}00")
            with_time += 1
        else:
            nxt = date(y, mo, d) + timedelta(days=1)
            out.append(f"DTSTART;VALUE=DATE:{y:04d}{mo:02d}{d:02d}")
            out.append(f"DTEND;VALUE=DATE:{nxt.strftime('%Y%m%d')}")
        out.append(fold(f"SUMMARY:{esc(title)}"))
        loc = build_location(ev["location"], meta)
        if loc:
            out.append(fold(f"LOCATION:{esc(loc)}"))
        out.append(fold(f"URL:{url}"))
        out.append(fold(f"DESCRIPTION:{esc(build_description(url, meta))}"))
        out.append("END:VEVENT")
    out.append("END:VCALENDAR")

    with open("skape.ics", "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(out) + "\r\n")

    enriched_count = sum(1 for e in events if e["uid"] in cache)
    print(
        f"Wrote skape.ics: {len(events)} events "
        f"({with_time} with times, {enriched_count} enriched)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
