"""Date/deadline phrase normalization for attention views."""

from __future__ import annotations

import os
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_request_timezone: ContextVar[Optional[ZoneInfo]] = ContextVar(
    "life_inbox_request_timezone", default=None
)


def configured_timezone() -> ZoneInfo:
    bound = _request_timezone.get()
    if bound is not None:
        return bound
    name = os.getenv("LIFE_INBOX_TIMEZONE", "UTC").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        return ZoneInfo("UTC")


def bind_request_timezone(name: Optional[str]) -> Token:
    """Bind a validated browser timezone; absent headers use the server default."""
    zone = None
    if name is not None:
        try:
            zone = ZoneInfo(name.strip())
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("Timezone must be a valid IANA timezone name.") from exc
    return _request_timezone.set(zone)


def reset_request_timezone(token: Token) -> None:
    _request_timezone.reset(token)


def reference_date(timestamp: str, timezone_name: str) -> Optional[date]:
    """Convert a recorded instant in its recorded timezone, never guessing either."""
    try:
        captured = datetime.fromisoformat(timestamp)
        if captured.tzinfo is None or captured.utcoffset() is None:
            return None
        return captured.astimezone(ZoneInfo(timezone_name)).date()
    except (TypeError, ValueError, OverflowError, ZoneInfoNotFoundError):
        return None


def today_in_configured_tz() -> date:
    return datetime.now(configured_timezone()).date()


@dataclass
class ParsedTiming:
    original_phrase: str
    due_date: Optional[date]
    confidence: str  # high | medium | low | none
    needs_confirmation: bool
    normalized_iso: Optional[str]
    note: Optional[str] = None


_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_MONTH_NAME = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?"
)
_MDY = re.compile(
    rf"\b({_MONTH_NAME})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4,}})\b)?",
    re.I,
)
_DMY = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_NAME})\b(?:\s+(\d{{4,}})\b)?",
    re.I,
)
_IN_DAYS = re.compile(r"\bin\s+(\d+)\s+days?\b", re.I)
_WEEKDAY = re.compile(
    r"\b(next\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I,
)
_RELATIVE_DAY = re.compile(r"\b(today|tomorrow|yesterday)\b", re.I)
_WEEKEND = re.compile(r"\b(?:(?:this|next)\s+)?weekend\b", re.I)
_SLASH_DATE = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
_EXACT_TIME = re.compile(
    r"\b\d{4}-\d{1,2}-\d{1,2}T\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?\b|"
    r"\b(?:\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]\.?m\.?)?|"
    r"\d{1,2}(?:\.\d{2})?\s*[ap]\.?\s*m\.?|\d{1,2}\s*o['’]?clock|\d{1,2}h(?:\d{2})?|"
    r"at\s+\d{1,2}(?:\s*o['’]?clock)?|"
    r"noon|midnight|morning|afternoon|evening|tonight|night|"
    r"in\s+\d+\s+(?:hours?|minutes?|seconds?))\b",
    re.I,
)
_VAGUE_TIME = re.compile(
    r"\b(?:this\s+week|next\s+week|soon|later|asap|as\s+soon\s+as\s+possible)\b",
    re.I,
)
_UNCERTAIN = re.compile(r"\b(?:maybe|perhaps|possibly|sometime|approximately|around)\b", re.I)
_RECURRENCE = re.compile(r"\b(?:every|each)\s+(?:day|week|month|year|\w+day)\b", re.I)


def split_timing_sentences(text: str) -> list[str]:
    """Separate action sentences without truncating dotted time expressions."""
    protected = set()
    for pattern in (r"(?<![A-Za-z])[ap]\.\s*m\.?", r"\b\d{1,2}\.\d{2}\s*[ap]\.?\s*m\.?"):
        for match in re.finditer(pattern, text, re.I):
            protected.update(
                index for index in range(match.start(), match.end())
                if text[index] == "."
            )
    parts = []
    start = 0
    for boundary in re.finditer(r"[.!?\n]", text):
        if boundary.start() in protected:
            continue
        part = text[start:boundary.start()].strip()
        if part:
            parts.append(part)
        start = boundary.end()
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _date_matches(text: str) -> list[tuple[str, re.Match]]:
    matches = []
    for kind, pattern in (
        ("iso", _ISO), ("mdy", _MDY), ("dmy", _DMY), ("days", _IN_DAYS),
        ("weekday", _WEEKDAY), ("relative", _RELATIVE_DAY), ("weekend", _WEEKEND),
    ):
        matches.extend((kind, match) for match in pattern.finditer(text))
    return sorted(matches, key=lambda pair: pair[1].start())


def extract_timing_phrase(text: str) -> Optional[str]:
    """Keep the timing suffix verbatim, including unsupported times/invalid dates.

    This is extraction, not interpretation. In particular, dropping "at 10am"
    from "tomorrow at 10am" would incorrectly turn it into a certain all-day date.
    """
    matches = [match for _, match in _date_matches(text)]
    for pattern in (_EXACT_TIME, _SLASH_DATE, _VAGUE_TIME):
        matches.extend(pattern.finditer(text))
    if not matches:
        return None
    # Keep qualifiers attached to timing. Dropping 'maybe' or 'not' would turn
    # an uncertain source edit into a confident 'today' instruction.
    for pattern in (_UNCERTAIN, _RECURRENCE, re.compile(r"\b(?:not|except|before|until|after|no\s+longer)\b", re.I)):
        matches.extend(pattern.finditer(text))
    start = min(match.start() for match in matches)
    prefix = re.search(r"\b(?:(?:needed|due)\s+)?(?:by|on|before|until|at)\s+$", text[:start], re.I)
    if prefix:
        start = prefix.start()
    return text[start:].strip() or None


def timing_intent_signature(text: Optional[str], *, unique: bool = False) -> tuple:
    """Compare timing expressions, excluding amounts and unrelated suffix text.

    Keep literal temporal intent: 'today' and 'in 0 days' are different edit
    instructions even if they resolve to the same date at the old reference.
    """
    result = []
    for clause in split_timing_sentences(text or ""):
        matches = [(match.start(), kind, match.group(1) if kind == "days" else match.group())
                   for kind, match in _date_matches(clause)]
        for kind, pattern in (("time", _EXACT_TIME), ("numeric", _SLASH_DATE),
                              ("vague", _VAGUE_TIME), ("recurrence", _RECURRENCE)):
            matches.extend((match.start(), kind, match.group()) for match in pattern.finditer(clause))
        if not matches:
            continue
        for kind, pattern in (
            ("uncertain", _UNCERTAIN),
            ("boundary", re.compile(r"\b(?:before|until|after|not|except|no\s+longer)\b", re.I)),
        ):
            matches.extend((match.start(), kind, match.group()) for match in pattern.finditer(clause))
        result.extend((kind, " ".join(value.lower().split())) for _, kind, value in sorted(matches))
    result = tuple(result)
    return tuple(sorted(set(result))) if unique else result

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def parse_timing_phrase(
    phrase: Optional[str],
    *,
    reference: Optional[date] = None,
) -> ParsedTiming:
    original = (phrase or "").strip()
    if not original:
        return ParsedTiming(
            original_phrase="",
            due_date=None,
            confidence="none",
            needs_confirmation=False,
            normalized_iso=None,
            note="No timing phrase.",
        )

    def uncertain(note: str) -> ParsedTiming:
        return ParsedTiming(original, None, "low", True, None, note)

    if _EXACT_TIME.search(original):
        return uncertain(
            "Exact times are not scheduled here. Choose a date-only deadline; "
            "the original time wording is preserved."
        )
    if _SLASH_DATE.search(original):
        return uncertain("Numeric slash dates are ambiguous. Choose a calendar date.")
    if _RECURRENCE.search(original):
        return uncertain("Recurring timing needs review. Choose a single calendar date.")
    if re.search(r"\b(?:not|except|no\s+longer)\b", original, re.I):
        return uncertain("Negated timing needs review. Choose a calendar date.")

    ref = reference or today_in_configured_tz()
    matches = _date_matches(original)
    if not matches:
        return uncertain("No unambiguous calendar date. Urgency does not set a deadline.")

    candidates: list[date] = []
    notes: list[str] = []
    try:
        for kind, match in matches:
            if kind == "iso":
                due = date(*(int(value) for value in match.groups()))
            elif kind == "relative":
                offset = {"yesterday": -1, "today": 0, "tomorrow": 1}[match.group(1).lower()]
                due = ref + timedelta(days=offset)
            elif kind == "days":
                due = ref + timedelta(days=int(match.group(1)))
            elif kind in {"mdy", "dmy"}:
                month_text, day_text, year_text = match.groups()
                if kind == "dmy":
                    day_text, month_text = month_text, day_text
                year = int(year_text) if year_text else ref.year
                month = _MONTHS[month_text.lower()]
                due = date(year, month, int(day_text))
                if not year_text:
                    if due < ref:
                        due = date(year + 1, month, int(day_text))
                    notes.append("Year inferred; choose the intended calendar date.")
            elif kind == "weekend":
                days_ahead = (5 - ref.weekday()) % 7
                if match.group().lower().startswith("next"):
                    days_ahead += 7
                due = ref + timedelta(days=days_ahead)
                notes.append("Weekend suggests Saturday; confirm the intended date.")
            else:
                target = _WEEKDAYS[match.group(2).lower()]
                days_ahead = (target - ref.weekday()) % 7
                if not days_ahead:
                    days_ahead = 7
                elif match.group(1):
                    days_ahead += 7
                due = ref + timedelta(days=days_ahead)
                notes.append("Weekday timing is ambiguous; confirm the intended date.")
            candidates.append(due)
    except (ValueError, OverflowError):
        return uncertain("Invalid or unsupported calendar date. Choose a valid date.")

    if len(set(candidates)) > 1:
        return uncertain("Multiple possible dates were mentioned. Choose the intended deadline.")
    if _UNCERTAIN.search(original):
        notes.append("The wording is uncertain; confirm the intended date.")
    if re.search(r"\b(?:before|until|after)\b", original, re.I):
        notes.append("Confirm which calendar day the boundary wording means.")
    due = candidates[0]
    return ParsedTiming(
        original_phrase=original,
        due_date=due,
        confidence="medium" if notes else "high",
        needs_confirmation=bool(notes),
        normalized_iso=due.isoformat(),
        note=" ".join(dict.fromkeys(notes)) or None,
    )


def classify_attention_bucket(
    due: Optional[date],
    *,
    reference: Optional[date] = None,
    snoozed_until: Optional[date] = None,
) -> str:
    ref = reference or today_in_configured_tz()
    if snoozed_until and snoozed_until > ref:
        return "snoozed"
    if due is None:
        return "unscheduled"
    if due < ref:
        return "overdue"
    if due == ref:
        return "today"
    return "upcoming"
