"""Date-only interpretation keeps its reference and exposes uncertainty."""

from datetime import date

import pytest

from app.attention.dates import (
    bind_request_timezone,
    configured_timezone,
    extract_timing_phrase,
    parse_timing_phrase,
    reference_date,
    reset_request_timezone,
    split_timing_sentences,
)


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("needed by today", "2026-10-15"),
        ("urgent tomorrow", "2026-10-16"),
        ("yesterday", "2026-10-14"),
        ("in 2 days", "2026-10-17"),
        ("2026-09-01", "2026-09-01"),
        ("October 15, 2026", "2026-10-15"),
        ("15 October 2026", "2026-10-15"),
    ],
)
def test_explicit_reference_resolves_calendar_dates_without_priority(phrase, expected):
    parsed = parse_timing_phrase(phrase, reference=date(2026, 10, 15))
    assert parsed.original_phrase == phrase
    assert parsed.normalized_iso == expected
    assert parsed.confidence == "high"
    assert parsed.needs_confirmation is False


@pytest.mark.parametrize(
    "phrase",
    [
        "2026-02-30", "0000-01-01", "2026-13-01", "February 30, 2026", "May 15 10000",
        "in 99999999999999999999999999999 days", "urgent", "ASAP",
        "today or tomorrow", "today, due on 2026-12-01", "10/11/2026",
        "every Friday", "not today",
    ],
)
def test_invalid_or_conflicting_wording_never_invents_a_precise_date(phrase):
    parsed = parse_timing_phrase(phrase, reference=date(2026, 10, 15))
    assert parsed.original_phrase == phrase
    assert parsed.due_date is None
    assert parsed.normalized_iso is None
    assert parsed.needs_confirmation
    assert parsed.note


@pytest.mark.parametrize(
    "phrase",
    ["tomorrow at 10am", "today at 10:00", "2026-10-15T10:30:00Z",
     "Friday morning", "tomorrow at noon", "today at 10", "in 2 hours",
     "tomorrow 9 o'clock", "tomorrow 9h30", "tomorrow at 10a.m.",
     "tomorrow 9.30p.m.", "tomorrow 10 a. m."],
)
def test_exact_time_wording_is_preserved_without_becoming_an_all_day_deadline(phrase):
    parsed = parse_timing_phrase(phrase, reference=date(2026, 10, 15))
    assert parsed.original_phrase == phrase
    assert parsed.due_date is None
    assert parsed.needs_confirmation
    assert "Exact times" in parsed.note


@pytest.mark.parametrize("phrase", ["Friday", "next Friday", "October 20", "this weekend maybe"])
def test_ambiguous_wording_requires_review_even_when_a_candidate_exists(phrase):
    parsed = parse_timing_phrase(phrase, reference=date(2026, 10, 15))
    assert parsed.due_date is not None
    assert parsed.needs_confirmation
    assert parsed.confidence == "medium"
    assert parsed.note


def test_date_boundaries_and_year_inference_cannot_crash():
    assert parse_timing_phrase("tomorrow", reference=date.max).due_date is None
    assert parse_timing_phrase("yesterday", reference=date.min).due_date is None
    # A yearless leap day cannot silently become an invalid date next year.
    assert parse_timing_phrase("February 29", reference=date(2024, 3, 1)).due_date is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Buy milk tomorrow", "tomorrow"),
        ("Pay bill needed by today", "needed by today"),
        ("Pay bill tomorrow at 10am", "tomorrow at 10am"),
        ("Pay bill on 2026-02-30", "on 2026-02-30"),
        ("Pay bill before Friday", "before Friday"),
        ("Urgent: pay the bill", None),
    ],
)
def test_extraction_keeps_useful_original_wording(text, expected):
    assert extract_timing_phrase(text) == expected


@pytest.mark.parametrize(
    ("timestamp", "zone", "expected"),
    [
        ("2026-10-14T20:00:00+00:00", "Asia/Kolkata", date(2026, 10, 15)),
        ("2026-10-14T20:00:00+00:00", "UTC", date(2026, 10, 14)),
        ("2026-10-15T00:30:00Z", "America/Los_Angeles", date(2026, 10, 14)),
        ("2026-03-08T06:30:00+00:00", "America/New_York", date(2026, 3, 8)),
        ("2026-03-08T07:30:00+00:00", "America/New_York", date(2026, 3, 8)),
        ("2026-11-01T05:30:00+00:00", "America/New_York", date(2026, 11, 1)),
        ("2026-11-01T06:30:00+00:00", "America/New_York", date(2026, 11, 1)),
    ],
)
def test_reference_uses_recorded_timezone_across_midnight_and_dst(timestamp, zone, expected):
    reference = reference_date(timestamp, zone)
    assert reference == expected
    parsed = parse_timing_phrase("today", reference=reference)
    assert parsed.due_date == expected


@pytest.mark.parametrize(
    ("timestamp", "zone"),
    [("2026-10-15T10:00:00", "UTC"), ("2026-10-15", "UTC"),
     ("bad", "UTC"), ("2026-10-15T10:00:00Z", "Unknown/Timezone"),
     (None, "UTC"), ("2026-10-15T10:00:00Z", None)],
)
def test_unknown_historical_reference_is_not_assumed(timestamp, zone):
    assert reference_date(timestamp, zone) is None


def test_request_timezone_binding_is_validated_and_reset(monkeypatch):
    monkeypatch.setenv("LIFE_INBOX_TIMEZONE", "UTC")
    outer = bind_request_timezone("Asia/Kolkata")
    try:
        assert str(configured_timezone()) == "Asia/Kolkata"
        inner = bind_request_timezone("America/New_York")
        reset_request_timezone(inner)
        assert str(configured_timezone()) == "Asia/Kolkata"
        with pytest.raises(ValueError, match="IANA timezone"):
            bind_request_timezone("Mars/Olympus")
        assert str(configured_timezone()) == "Asia/Kolkata"
    finally:
        reset_request_timezone(outer)
    assert str(configured_timezone()) == "UTC"


def test_legacy_browser_timezone_names_resolve_without_system_tzdata():
    # Slim Docker images drop backward-compatible names such as Asia/Calcutta,
    # which Chrome on macOS still reports; the tzdata package must supply them.
    import zoneinfo

    zoneinfo.reset_tzpath(to=[])
    zoneinfo.ZoneInfo.clear_cache()
    try:
        token = bind_request_timezone("Asia/Calcutta")
        try:
            assert str(configured_timezone()) == "Asia/Calcutta"
        finally:
            reset_request_timezone(token)
    finally:
        zoneinfo.reset_tzpath()
        zoneinfo.ZoneInfo.clear_cache()


def test_fallback_does_not_borrow_a_sibling_action_deadline():
    from app.organizer import fallback_analyze

    result = fallback_analyze([
        {"id": 1, "text": "Call the dentist. Pay the electricity bill tomorrow at 10am."}
    ])
    assert result["todos"][0]["when"] is None
    assert result["todos"][1]["when"] == "tomorrow at 10am"
    parsed = parse_timing_phrase(result["todos"][1]["when"], reference=date(2026, 10, 15))
    assert parsed.due_date is None and parsed.needs_confirmation


@pytest.mark.parametrize("timing", ["tomorrow at 10a.m.", "tomorrow 9.30p.m.", "tomorrow 10 a. m."])
def test_sentence_extraction_cannot_turn_dotted_times_into_date_only(timing):
    from app.organizer import fallback_analyze

    text = f"Pay the bill {timing} Call the dentist."
    parts = split_timing_sentences(text)
    assert timing in parts[0]
    task = fallback_analyze([{"id": 1, "text": text}])["todos"][0]
    assert timing in task["when"]
    parsed = parse_timing_phrase(task["when"], reference=date(2026, 10, 15))
    assert parsed.due_date is None and parsed.needs_confirmation
