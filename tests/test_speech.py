import pytest

import speech


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  hello there  ", "hello there"),
        ("hello    there", "hello there"),
        ("", ""),
        ("   \t  ", ""),
        (None, ""),
    ],
)
def test_whitespace_is_collapsed(raw, expected):
    assert speech.sanitize(raw, 200) == expected


@pytest.mark.parametrize("raw", ["box\nthis lap", "box\r\nthis lap", "box\rthis lap"])
def test_newlines_become_spaces(raw):
    """A stray newline would submit the message halfway through typing it."""
    assert speech.sanitize(raw, 200) == "box this lap"


def test_truncation_respects_max_chars():
    assert speech.sanitize("abcdefghij", 4) == "abcd"


def test_truncation_does_not_leave_a_trailing_space():
    assert speech.sanitize("ab cdefgh", 3) == "ab"


def test_max_chars_zero_means_no_limit():
    assert speech.sanitize("abcdefghij", 0) == "abcdefghij"


def test_text_shorter_than_the_limit_is_untouched():
    assert speech.sanitize("box this lap", 200) == "box this lap"


def test_non_ascii_survives():
    """Whisper emits smart quotes and dashes routinely."""
    assert speech.sanitize("a “quote” and em—dash", 200) == "a “quote” and em—dash"
