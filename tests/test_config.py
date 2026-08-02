import json

import config


def _base() -> dict:
    return json.loads(
        (__import__("pathlib").Path(__file__).parent.parent / "config.default.json")
        .read_text(encoding="utf-8")
    )


# -- the shipped config --------------------------------------------------


def test_shipped_config_is_valid():
    cfg = config.Config.from_dict(_base())
    assert cfg.validate() == []


def test_shipped_config_has_only_le_mans_ultimate():
    """Speculative profiles for unverified sims are worse than none."""
    cfg = config.Config.from_dict(_base())
    assert list(cfg.profiles) == ["le mans ultimate.exe"]


# -- profile resolution --------------------------------------------------


def test_profile_lookup_is_case_insensitive():
    cfg = config.Config.from_dict(_base())
    profile, matched = cfg.profile_for("Le Mans Ultimate.exe")
    assert matched == "le mans ultimate.exe"
    assert profile.pre_keys == ["enter"]


def test_unknown_executable_falls_back_to_default():
    cfg = config.Config.from_dict(_base())
    _profile, matched = cfg.profile_for("notepad.exe")
    assert matched == "default"

    _profile, matched = cfg.profile_for(None)
    assert matched == "default"


def test_profile_inherits_unspecified_fields_from_default():
    raw = _base()
    raw["default_profile"]["type_delay_ms"] = 42
    raw["profiles"]["game.exe"] = {"pre_keys": ["t"]}

    cfg = config.Config.from_dict(raw)
    profile, _ = cfg.profile_for("game.exe")
    assert profile.pre_keys == ["t"]          # overridden
    assert profile.type_delay_ms == 42        # inherited
    assert profile.abort_keys == ["escape"]   # inherited


def test_profile_override_does_not_leak_into_default():
    raw = _base()
    raw["profiles"]["game.exe"] = {"max_chars": 60}
    cfg = config.Config.from_dict(raw)
    assert cfg.profile_for("game.exe")[0].max_chars == 60
    assert cfg.default_profile.max_chars == 200


def test_unknown_keys_in_the_file_are_ignored():
    raw = _base()
    raw["whisper"]["some_future_option"] = True
    raw["totally_unknown_section"] = {"x": 1}
    cfg = config.Config.from_dict(raw)
    assert cfg.validate() == []


def test_missing_sections_fall_back_to_defaults():
    cfg = config.Config.from_dict({})
    assert cfg.trigger_key == "f13"
    assert cfg.whisper.device == "cpu"
    assert cfg.validate() == []


# -- validation ----------------------------------------------------------


def _problems(mutate) -> list[str]:
    raw = _base()
    mutate(raw)
    return config.Config.from_dict(raw).validate()


def test_trigger_key_with_modifier_is_rejected():
    problems = _problems(lambda r: r.update(trigger_key="ctrl+f13"))
    assert any("trigger_key" in p for p in problems)


def test_unknown_key_name_is_reported():
    def mutate(r):
        r["default_profile"]["pre_keys"] = ["enter", "wat"]

    assert any("unknown key" in p for p in _problems(mutate))


def test_gpu_device_is_rejected():
    """CPU-only is a deliberate choice: the GPU belongs to the sim."""
    problems = _problems(lambda r: r["whisper"].update(device="cuda"))
    assert any("CPU-only" in p for p in problems)


def test_short_key_hold_is_flagged():
    """Games poll input once per frame; a 5ms press is never observed."""
    def mutate(r):
        r["default_profile"]["key_hold_ms"] = 5

    assert any("key_hold_ms" in p for p in _problems(mutate))


def test_non_16k_samplerate_is_flagged():
    problems = _problems(lambda r: r["audio"].update(samplerate=44100))
    assert any("samplerate" in p for p in problems)


def test_bad_text_mode_is_rejected():
    def mutate(r):
        r["default_profile"]["text_mode"] = "magic"

    assert any("text_mode" in p for p in _problems(mutate))


def test_profile_key_without_exe_suffix_is_flagged():
    def mutate(r):
        r["profiles"]["notanexe"] = {}

    assert any("executable name" in p for p in _problems(mutate))


def test_negative_delay_is_rejected():
    def mutate(r):
        r["default_profile"]["pre_delay_ms"] = -1

    assert any("pre_delay_ms" in p for p in _problems(mutate))


def test_bad_update_repo_is_rejected():
    problems = _problems(lambda r: r["updates"].update(repo="justaname"))
    assert any("owner/name" in p for p in problems)


# -- persistence and reload ----------------------------------------------


def test_save_load_round_trip(tmp_path):
    cfg = config.Config.from_dict(_base())
    cfg.default_profile.pre_delay_ms = 555
    cfg.profiles["game.exe"] = config.Profile(pre_keys=["t"])

    path = tmp_path / "config.json"
    config.save(path, cfg)
    reloaded = config.load(path)

    assert reloaded.default_profile.pre_delay_ms == 555
    assert reloaded.profiles["game.exe"].pre_keys == ["t"]
    assert reloaded.to_dict() == cfg.to_dict()


def test_save_leaves_no_temp_file_behind(tmp_path):
    """The write is atomic so a hot-reload can't read a half-written file."""
    path = tmp_path / "config.json"
    config.save(path, config.Config())
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_store_reloads_when_the_file_changes(tmp_path):
    path = tmp_path / "config.json"
    cfg = config.Config.from_dict(_base())
    config.save(path, cfg)

    store = config.ConfigStore(path)
    store.load()
    assert store.config.default_profile.pre_delay_ms == 350
    assert store.maybe_reload() is False

    cfg.default_profile.pre_delay_ms = 900
    config.save(path, cfg)
    import os
    os.utime(path, (0, 0))  # force a distinct mtime rather than sleeping

    assert store.maybe_reload() is True
    assert store.config.default_profile.pre_delay_ms == 900


def test_missing_file_falls_back_to_defaults(tmp_path):
    store = config.ConfigStore(tmp_path / "nope.json")
    store.load()
    assert store.config.trigger_key == "f13"
    assert any("not found" in p for p in store.problems)


def test_malformed_json_keeps_the_previous_config(tmp_path):
    """A bad edit mid-session must not take the running app down."""
    path = tmp_path / "config.json"
    cfg = config.Config.from_dict(_base())
    cfg.default_profile.pre_delay_ms = 777
    config.save(path, cfg)

    store = config.ConfigStore(path)
    store.load()

    path.write_text("{ this is not json", encoding="utf-8")
    store.load()

    assert store.config.default_profile.pre_delay_ms == 777
    assert any("could not be parsed" in p for p in store.problems)
