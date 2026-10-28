"""Deterministic parsers for front-desk conversations (fast, testable, no LLM)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from careharbor.tools.service import parse_dob

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAY_ABBR = {d[:3]: i for i, d in enumerate(WEEKDAYS)} | {"tues": 1, "thur": 3, "thurs": 3}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "last": -1}

YES = re.compile(r"(?i)^\W*(yes|yeah|yep|yup|ya|haan|han|ha|ji|sure|ok|okay|okk|confirm(ed)?|go ahead|book it|do it|correct|right|that's right|please do|theek hai|thik hai)\b")
NO = re.compile(r"(?i)^\W*(no|nope|nah|nahi|nahin|don't|do not|cancel( it| that)?|stop|never mind|nevermind|not now|leave it|rehne do)\b")
CHANGE = re.compile(r"(?i)\b(change|different|another|other (time|day|slot|doctor)|later|earlier|something else|none of (these|them)|not these)\b")


def find_phone(text: str) -> str | None:
    m = re.search(r"(?<!\d)(?:\+?91[\s-]?)?([6-9]\d{4}[\s-]?\d{5})(?!\d)", text)
    return re.sub(r"\D", "", m.group(1)) if m else None


def find_dob(text: str) -> str | None:
    candidates = re.findall(
        r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}\b|\b\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9},?\s+\d{4}\b|\b[A-Za-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b",
        text,
    )
    for c in candidates:
        if (d := parse_dob(c)) is not None:
            return d
    return None


def find_otp(text: str) -> str | None:
    if find_phone(text):
        return None
    m = re.search(r"(?<![\d-])(\d{3})[\s-]?(\d{3})(?![\d-])", text)
    return m.group(1) + m.group(2) if m else None


@dataclass
class DateRange:
    start: date
    end: date
    label: str


def parse_date_pref(text: str, today: date) -> DateRange | None:
    t = text.lower()
    if re.search(r"\bday after tomorrow\b|\bparso\b", t):
        d = today + timedelta(days=2)
        return DateRange(d, d, "day after tomorrow")
    if re.search(r"\btomorrow\b|\btmrw\b|\btmr\b|\bkal\b", t):
        d = today + timedelta(days=1)
        return DateRange(d, d, "tomorrow")
    if re.search(r"\btoday\b|\baaj\b|\btonight\b", t):
        return DateRange(today, today, "today")
    if m := re.search(r"\bin (\d{1,2}) days?\b", t):
        d = today + timedelta(days=int(m.group(1)))
        return DateRange(d, d, f"in {m.group(1)} days")
    if re.search(r"\bnext week\b|\bagle hafte\b", t):
        start = today + timedelta(days=7 - today.weekday())
        return DateRange(start, start + timedelta(days=5), "next week")
    if re.search(r"\bthis week\b", t):
        return DateRange(today, today + timedelta(days=6 - today.weekday()), "this week")
    if m := re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t):
        try:
            d = date(int(m[1]), int(m[2]), int(m[3]))
            return DateRange(d, d, d.strftime("%a %d %b"))
        except ValueError:
            pass
    if m := re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]{3})[a-z]*\b|\b([a-z]{3})[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b", t):
        day_s, mon_s = (m[1], m[2]) if m[1] else (m[4], m[3])
        if mon_s in MONTHS:
            try:
                d = date(today.year, MONTHS[mon_s], int(day_s))
                if d < today:
                    d = date(today.year + 1, d.month, d.day)
                return DateRange(d, d, d.strftime("%a %d %b"))
            except ValueError:
                pass
    if m := re.search(r"(?<![\d/.])(\d{1,2})[/.](\d{1,2})(?![\d/.])", t):
        try:
            d = date(today.year, int(m[2]), int(m[1]))
            if d < today:
                d = date(today.year + 1, d.month, d.day)
            return DateRange(d, d, d.strftime("%a %d %b"))
        except ValueError:
            pass
    for word in re.findall(r"[a-z]+", t):
        idx = WEEKDAYS.index(word) if word in WEEKDAYS else WEEKDAY_ABBR.get(word)
        if idx is not None:
            ahead = (idx - today.weekday()) % 7
            if re.search(rf"\bnext {word}\b", t):
                ahead = ahead or 7
            d = today + timedelta(days=ahead)
            return DateRange(d, d, WEEKDAYS[idx].title())
    return None


def parse_time_pref(text: str) -> tuple[str, str] | None:
    t = text.lower()
    if re.search(r"\bmorning\b|\bsubah\b", t):
        return ("00:00", "12:00")
    if re.search(r"\bafternoon\b|\bdopahar\b|\bafter lunch\b", t):
        return ("12:00", "16:00")
    if re.search(r"\bevening\b|\bshaam\b", t):
        return ("16:00", "23:59")
    if m := re.search(r"\b(?:at|around|by|after)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t):
        h = int(m[1]) % 12 + (12 if m[3] == "pm" else 0)
        return (f"{max(h - 1, 0):02d}:{m[2] or '00'}", f"{min(h + 1, 23):02d}:{m[2] or '59'}")
    return None


def parse_choice(text: str, n_options: int, option_times: list[str] | None = None) -> int | None:
    """Return a 0-based option index from '2', 'option 2', 'the second one', '10:15', etc."""
    t = text.lower().strip()
    if m := re.fullmatch(r"\D{0,12}?(\d)\D{0,12}", t):
        i = int(m.group(1))
        if 1 <= i <= n_options:
            return i - 1
    if m := re.search(r"\b(?:option|slot|number|no\.?|#)\s*(\d)\b", t):
        i = int(m.group(1))
        if 1 <= i <= n_options:
            return i - 1
    for word, i in ORDINALS.items():
        if re.search(rf"\b{word}\b", t):
            idx = n_options - 1 if i == -1 else i - 1
            if 0 <= idx < n_options:
                return idx
    if option_times and (m := re.search(r"\b(\d{1,2})[:.](\d{2})\b", t)):
        want = (int(m[1]), int(m[2]))
        for i, ts in enumerate(option_times):
            d = datetime.fromisoformat(ts)
            if want in {(d.hour, d.minute), (d.hour - 12, d.minute)}:
                return i
    return None


def yes_no(text: str) -> str | None:
    if YES.search(text):
        return "yes"
    if NO.search(text):
        return "no"
    return None


DOCTOR_NAME_KEYS = {
    "D01": "rao", "D02": "iyer", "D03": "siddiqui", "D04": "reddy", "D05": "kulkarni", "D06": "narayanan", "D07": "mehta", "D08": "desai",
}
SAME_DOCTOR = re.compile(r"(?i)\b(again|same doctor|same one|last time|previous doctor|my doctor|who i saw|whom i saw|i visited|i saw)\b")


def find_doctor_by_name(text: str) -> str | None:
    words = set(re.findall(r"[a-z]+", text.lower()))
    for doc_id, surname in DOCTOR_NAME_KEYS.items():
        if surname in words:
            return doc_id
    return None


def fmt_slot(ts: str) -> str:
    """'2026-11-06T09:15' -> 'Fri 6 Nov, 9:15 AM'"""
    d = datetime.fromisoformat(ts)
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}, {d.strftime('%I:%M %p').lstrip('0')}"
