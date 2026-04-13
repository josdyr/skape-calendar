#!/usr/bin/env python3
"""Scrape skape.no/kurs/kurskalender and emit skape.ics."""
from __future__ import annotations

import hashlib
import re
import sys
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

LIST_URL = "https://www.skape.no/kurs/kurskalender"
UA = "skape-calendar-bot (+https://github.com/josdyr/skape-calendar)"

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


def fetch_time_window(session, url):
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
    except requests.RequestException:
        return None
    m = TIME_RE.search(r.text)
    if not m:
        return None
    h1, m1, h2, m2 = map(int, m.groups())
    return (h1, m1), (h2, m2)


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
        ymd = parse_list_date(date_el.get_text(" ", strip=True))
        if not ymd:
            continue
        url = urljoin(LIST_URL, a["href"])
        title = title_el.get_text(" ", strip=True)
        location = loc_el.get_text(" ", strip=True) if loc_el else ""
        tw = fetch_time_window(session, url)
        events.append((url, title, location, ymd, tw))

    if not events:
        print("ERROR: parsed 0 events — HTML structure may have changed", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//josdyr/skape-calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Skape kurskalender",
        "X-WR-TIMEZONE:Europe/Oslo",
        "X-PUBLISHED-TTL:PT6H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        *VTIMEZONE,
    ]
    with_time = 0
    for url, title, location, (y, mo, d), tw in events:
        uid = hashlib.sha1(url.encode("utf-8")).hexdigest() + "@skape-calendar"
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
        if location:
            out.append(fold(f"LOCATION:{esc(location)}"))
        out.append(fold(f"URL:{url}"))
        out.append(fold(f"DESCRIPTION:{esc('Les mer: ' + url)}"))
        out.append("END:VEVENT")
    out.append("END:VCALENDAR")

    with open("skape.ics", "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(out) + "\r\n")

    print(f"Wrote skape.ics: {len(events)} events ({with_time} with times, {len(events) - with_time} all-day)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
