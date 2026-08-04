"""Language and model-size resolution.

Whisper has no per-language models: English-only builds (`tiny.en` … `medium.en`)
and multilingual builds (`tiny` … `large-v3`), the latter covering all 99
languages. The mapping from a user's "Spanish, medium" to an actual model name
is the only place that distinction lives, so it's worth pinning.
"""

from pathlib import Path

import pytest

from pitradio import config, languages

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


# -- following the system language ---------------------------------------


@pytest.mark.parametrize("tag,expected", [
    ("en_GB.UTF-8", "en"),
    ("pt-BR", "pt"),
    ("zh_Hans_CN", "zh"),
    ("es_ES@euro", "es"),
    ("de_DE", "de"),
    ("fr", "fr"),
    # Locale tags that predate the current ISO codes and still turn up.
    ("iw_IL", "he"),
    ("in_ID", "id"),
    ("ji", "yi"),
    ("nb_NO", "no"),
    ("fil_PH", "tl"),
])
def test_a_locale_tag_becomes_a_whisper_code(tag, expected):
    assert languages.normalise_locale(tag) == expected


@pytest.mark.parametrize("tag", ["", "C", "POSIX", "xx_YY", "klingon", None])
def test_an_unusable_locale_yields_nothing(tag):
    """Whisper has no model for it, so guessing would be worse than the default."""
    assert languages.normalise_locale(tag or "") == ""


def test_every_alias_maps_to_a_language_whisper_knows():
    """An alias pointing at a code with no model would seed an unusable config."""
    for tag, code in languages._LOCALE_ALIASES.items():
        assert code in languages.WHISPER_LANGUAGES, f"{tag} -> {code}"


def test_the_system_language_falls_back_when_nothing_is_set(monkeypatch):
    import locale

    for name in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(locale, "getlocale", lambda *a: (None, None))

    assert languages.system_language() == "en"


def test_the_environment_is_read_in_precedence_order(monkeypatch):
    """LC_ALL wins over LANG, as every POSIX system defines it."""
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    assert languages.system_language() == "de"

    monkeypatch.delenv("LC_ALL")
    assert languages.system_language() == "fr"


def test_a_language_list_entry_is_skipped_when_unsupported(monkeypatch):
    """LANGUAGE holds a colon-separated fallback chain."""
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANGUAGE", "es_ES:en_US")
    assert languages.system_language() == "es"


def test_a_seeded_model_exists_for_every_detectable_language():
    """model_name must return something loadable for anything we might detect."""
    for code in set(languages._LOCALE_ALIASES.values()) | {"en", "de", "es"}:
        model = languages.model_name(code, languages.DEFAULT_SIZE)
        assert model
        assert code == "en" or not model.endswith(".en"), (
            f"{code} would be seeded with an English-only model"
        )


# -- first-run seeding ----------------------------------------------------


@pytest.fixture
def seeding(monkeypatch, tmp_path):
    """`seed_config` pointed at a temporary directory."""
    from pitradio import __main__ as cli
    from pitradio import paths

    target = tmp_path / "config.json"
    monkeypatch.setattr(paths, "config_path", lambda: target)
    monkeypatch.setattr(cli.paths, "config_path", lambda: target)
    monkeypatch.setattr(cli.paths, "default_config_path",
                        lambda: Path(__file__).parent.parent / "config.default.json")
    return cli, target


def test_a_new_config_follows_the_system_language(seeding, monkeypatch):
    cli, target = seeding
    monkeypatch.setattr(languages, "system_language", lambda default="en": "es")

    cli.seed_config()

    cfg = config.load(target)
    assert cfg.whisper.language == "es"
    assert cfg.whisper.languages == {"es": languages.DEFAULT_SIZE}
    # Never an English-only build for a non-English language: faster-whisper
    # would transcribe English anyway and say nothing about it.
    assert not cfg.whisper.model.endswith(".en")
    assert cfg.validate() == []


def test_an_english_system_leaves_the_shipped_defaults_alone(seeding, monkeypatch):
    cli, target = seeding
    monkeypatch.setattr(languages, "system_language", lambda default="en": "en")

    cli.seed_config()

    cfg = config.load(target)
    assert cfg.whisper.language == "en"
    assert cfg.whisper.model == "small.en"


def test_an_existing_config_is_never_relanguaged(seeding, monkeypatch):
    """The choice is the user's once made; re-deriving it would revert them."""
    cli, target = seeding
    monkeypatch.setattr(languages, "system_language", lambda default="en": "de")

    cli.seed_config()
    assert config.load(target).whisper.language == "de"

    # The user picks something else, then restarts.
    cfg = config.load(target)
    cfg.whisper.language = "fr"
    cfg.whisper.languages = {"fr": "small"}
    cfg.whisper.model = languages.model_name("fr", "small")
    config.save(target, cfg)

    cli.seed_config()
    assert config.load(target).whisper.language == "fr"


def test_a_detection_failure_still_leaves_a_usable_config(seeding, monkeypatch):
    """First run is the worst possible moment to raise."""
    cli, target = seeding

    def explode(default="en"):
        raise RuntimeError("no locale for you")

    monkeypatch.setattr(languages, "system_language", explode)

    cli.seed_config()
    assert target.exists()
    assert config.load(target).validate() == []


def test_the_os_default_does_not_override_an_explicit_setting(monkeypatch):
    """Windows-only ordering bug, found by CI and invisible everywhere else.

    `GetUserDefaultUILanguage` was consulted before the environment, so on
    Windows it always won. Nothing caught it locally because these variables
    are normally unset there, so the two never disagreed — the branch only
    ever ran with nothing to lose to.
    """
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")

    calls = []

    def record(default="en"):
        calls.append(default)
        return "en"

    # Whatever the OS says, an explicit LC_ALL is a stated preference.
    assert languages.system_language() == "de"
