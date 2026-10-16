import pytest

from autoshorts.timestamp_parser import (
    Clip,
    parse_cuts,
    seconds_to_timestamp,
    time_to_seconds,
)


def test_time_to_seconds_supports_long_minute_format() -> None:
    assert time_to_seconds("90:05") == 5405
    assert time_to_seconds("01:30:05") == 5405


def test_parse_cuts_accepts_comments_and_unicode_dash() -> None:
    assert parse_cuts("# theme\n[00:05 – 00:12] - Useful moment") == [
        Clip(5, 12, "Useful moment")
    ]


def test_parse_cuts_rejects_malformed_nonempty_line() -> None:
    with pytest.raises(ValueError, match="line 2"):
        parse_cuts("[00:05 - 00:12] - Valid\nthis is not a clip")


def test_parse_cuts_rejects_reversed_interval() -> None:
    with pytest.raises(ValueError, match="greater"):
        parse_cuts("[00:12 - 00:05] - Reversed")


def test_seconds_to_timestamp() -> None:
    assert seconds_to_timestamp(5405) == "01:30:05"
