"""Language and model-size resolution.

Whisper has no per-language models: English-only builds (`tiny.en` … `medium.en`)
and multilingual builds (`tiny` … `large-v3`), the latter covering all 99
languages. The mapping from a user's "Spanish, medium" to an actual model name
is the only place that distinction lives, so it's worth pinning.
"""

import pytest

import config
import languages

# -- model resolution ----------------------------------------------------


@pytest.mark.parametrize(
    ("language", "size", "expected"),
    [
        ("en", "tiny", "tiny.en"),
        ("en", "base", "base.en"),
        ("en", "small", "small.en"),
        ("en", "medium", "medium.en"),
        # No large.en exists, so English falls back to the multilingual build.
        ("en", "large", "large-v3"),
        ("es", "small", "small"),
        ("es", "medium", "medium"),
        ("de", "large", "large-v3"),
        ("ja", "tiny", "tiny"),
    ],
)
def test_model_name(language, size, expected):
    assert languages.model_name(language, size) == expected


def test_english_prefers_the_dedicated_build():
    """It is more accurate than multilingual at the same size, which is the
    entire reason English is special-cased."""
    assert languages.model_name("en", "small") == "small.en"
    assert languages.model_name("es", "small") == "small"


def test_language_case_and_padding_do_not_matter():
    assert languages.model_name(" EN ", "small") == "small.en"
    assert languages.model_name("en", " SMALL ") == "small.en"


def test_unknown_size_falls_back_to_the_default():
    assert languages.model_name("es", "enormous") == languages.DEFAULT_SIZE
    assert languages.model_name("en", "") == "small.en"


@pytest.mark.parametrize(
    ("model", "size"),
    [("small.en", "small"), ("medium.en", "medium"), ("large-v3", "large"),
     ("small", "small"), ("tiny", "tiny"), ("", "small")],
)
def test_size_of_inverts_model_name(model, size):
    assert languages.size_of(model) == size


@pytest.mark.parametrize("code", ["en", "es", "de", "ja", "yue"])
@pytest.mark.parametrize("size", list(languages.SIZES))
def test_every_combination_round_trips(code, size):
    """Whatever the tab writes must read back as the same size."""
    assert languages.size_of(languages.model_name(code, size)) == size


# -- labels --------------------------------------------------------------


def test_label_and_code_round_trip():
    for code in languages.WHISPER_LANGUAGES:
        assert languages.code_from_label(languages.label(code)) == code


def test_english_sorts_first():
    """It is the default and the one most users want; it should not be buried."""
    assert languages.sorted_labels()[0] == "English (en)"


def test_language_set_matches_faster_whisper_exactly():
    """Compared against the real thing rather than a remembered count.

    Whisper is usually described as supporting 99 languages; the tokenizer
    actually carries 100 codes. Asserting the folklore number would have
    encoded the wrong fact, and an unknown code passed to transcribe() is
    rejected at runtime — so this has to match the library, not a blog post.
    """
    tokenizer = pytest.importorskip(
        "faster_whisper.tokenizer", reason="faster-whisper not installed")

    assert set(languages.WHISPER_LANGUAGES) == set(tokenizer._LANGUAGE_CODES)


def test_the_common_languages_are_present():
    for code in ("en", "es", "de", "fr", "ja", "zh", "pt", "it"):
        assert code in languages.WHISPER_LANGUAGES
    assert len(languages.WHISPER_LANGUAGES) > 90


# -- config integration --------------------------------------------------


def test_default_config_configures_english():
    cfg = config.Config()
    assert cfg.whisper.languages == {"en": "small"}
    assert cfg.validate() == []


def test_multiple_languages_at_different_sizes_are_valid():
    cfg = config.Config.from_dict(
        {"whisper": {"languages": {"en": "small", "es": "medium"}, "language": "es",
                     "model": "medium"}}
    )
    assert cfg.validate() == []
    assert languages.model_name("es", cfg.whisper.languages["es"]) == "medium"
    assert languages.model_name("en", cfg.whisper.languages["en"]) == "small.en"


def test_unknown_language_code_is_rejected():
    cfg = config.Config.from_dict({"whisper": {"languages": {"xx": "small"}}})
    assert any("not a Whisper language" in p for p in cfg.validate())


def test_unknown_size_is_rejected():
    cfg = config.Config.from_dict({"whisper": {"languages": {"es": "gigantic"}}})
    assert any("whisper.languages['es']" in p for p in cfg.validate())


def test_languages_survive_a_round_trip(tmp_path):
    cfg = config.Config()
    cfg.whisper.languages = {"en": "small", "es": "medium", "ja": "large"}

    path = tmp_path / "config.json"
    config.save(path, cfg)
    assert config.load(path).whisper.languages == {
        "en": "small", "es": "medium", "ja": "large"
    }


def test_a_saved_active_language_agrees_with_its_model():
    """What the Language tab writes must pass the English-only check."""
    for code, size in (("en", "small"), ("es", "medium"), ("de", "large")):
        cfg = config.Config()
        cfg.whisper.languages = {code: size}
        cfg.whisper.language = code
        cfg.whisper.model = languages.model_name(code, size)
        assert cfg.validate() == [], f"{code}/{size} produced problems"
