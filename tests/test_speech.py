import pytest

from pitradio import speech


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


# -- picking a device out of Windows' several views of it -----------------


HISENSE_MME = "3 - HISENSE (2- AMD High Defini"
HISENSE_FULL = "3 - HISENSE (2- AMD High Definition Audio Device)"

#: What `sd.query_devices` reports for one HDMI output: index -> (name, api).
#: The MME row's name is 31 characters because that is all WaveOut's fixed
#: field holds, and it is truncated silently.
HDMI_OUT = {
    5: (HISENSE_MME, "MME"),
    14: (HISENSE_FULL, "Windows DirectSound"),
    19: (HISENSE_FULL, "Windows WASAPI"),
}


@pytest.fixture
def hdmi(monkeypatch):
    """One physical TV, as Windows lists it: once per host API."""
    monkeypatch.setattr(
        speech, "list_devices",
        lambda kind="input": [(index, f"[{index}] {name}")
                              for index, (name, _api) in sorted(HDMI_OUT.items())])
    monkeypatch.setattr(
        speech, "device_name",
        lambda index, kind="output": HDMI_OUT.get(index, ("", ""))[0])
    monkeypatch.setattr(
        speech, "_api_rank",
        lambda index, kind: speech._HOST_API_PREFERENCE.index(
            HDMI_OUT[index][1]))


def test_a_truncated_name_still_finds_the_wasapi_device(hdmi):
    """**The bug that made the TV go quiet.**

    The picker stored MME's 31-character truncation, so matching on equality
    could only ever find the MME row — and MME is the one host API whose writes
    succeed and produce no sound while a game holds the endpoint. Which is
    exactly the case when the TV is also the system default.
    """
    assert speech.resolve_device(HISENSE_MME, "output") == 19


def test_the_full_name_finds_it_too(hdmi):
    assert speech.resolve_device(HISENSE_FULL, "output") == 19


def test_the_picker_stores_the_fullest_name(hdmi):
    """Choosing the MME row must not write a truncation into the config."""
    assert speech.canonical_name(5, "output") == HISENSE_FULL


def test_a_short_name_is_not_matched_by_prefix(hdmi, monkeypatch):
    """Truncation is forgiven only at MME's limit. Otherwise "Speakers" would
    match "Speakers (Realtek)" and "Speakers (PRO X)" alike, and the choice
    between two real devices would come down to which was enumerated first."""
    assert not speech._matches("3 - hisense", HISENSE_FULL.lower())


def test_an_unplugged_device_falls_back_and_says_so(hdmi, caplog):
    with caplog.at_level("WARNING"):
        assert speech.resolve_device("Some Old Headset", "output") is None
    assert "system default" in caplog.text
