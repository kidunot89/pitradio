"""The Language tab: which languages to support, and at what model size.

Whisper has no per-language models. There are English-only builds and
multilingual ones, and every multilingual build covers every language Whisper knows. What
this tab configures is therefore a size *per language* — worth having because
multilingual `small` is weaker than `small.en`, so a second language often
wants a larger model than English does.

Saving derives `whisper.model` from the active language and its size, so the
worker and the transcriber keep loading one plain model name and know nothing
about any of this.
"""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from pitradio import languages as languages_mod
from pitradio import paths, speech
from pitradio.ui import gui_settings

log = logging.getLogger(__name__)


def build_language_tab(app) -> None:
    frame, footer = gui_settings.scrolling_tab(app, "Language")
    cfg = app.store.config

    ttk.Label(
        frame,
        text=("Whisper has no per-language models: the multilingual models cover "
              "every language Whisper knows, and English additionally has a dedicated build "
              "that is more accurate at the same size. Pick a size per language "
              "— a second language often wants a larger model than English."),
        foreground="#666", wraplength=880, justify="left",
    ).pack(fill="x", pady=(0, 10))

    body = ttk.Frame(frame)
    body.pack(fill="both", expand=True)

    left = ttk.LabelFrame(body, text="Languages", padding=8)
    left.pack(side="left", fill="both", expand=True)

    columns = ("language", "size", "model", "status")
    app.language_tree = ttk.Treeview(left, columns=columns, show="headings", height=10)
    for column, heading, width in (
        ("language", "Language", 170), ("size", "Size", 90),
        ("model", "Model", 120), ("status", "Downloaded", 110),
    ):
        app.language_tree.heading(column, text=heading)
        app.language_tree.column(column, width=width, anchor="w")
    app.language_tree.pack(fill="both", expand=True)

    controls = ttk.Frame(left)
    controls.pack(fill="x", pady=(8, 0))

    app.v_new_language = tk.StringVar(value=languages_mod.label("es"))
    ttk.Combobox(controls, textvariable=app.v_new_language, state="readonly",
                 values=languages_mod.sorted_labels(), width=26).pack(side="left")

    app.v_new_size = tk.StringVar(value=languages_mod.DEFAULT_SIZE)
    ttk.Combobox(controls, textvariable=app.v_new_size, state="readonly",
                 values=list(languages_mod.SIZES), width=9).pack(side="left", padx=6)

    ttk.Button(controls, text="Add / update",
               command=lambda: _add_language(app)).pack(side="left")
    ttk.Button(controls, text="Remove",
               command=lambda: _remove_language(app)).pack(side="left", padx=6)

    right = ttk.LabelFrame(body, text="Active language", padding=8)
    right.pack(side="left", fill="y", padx=(10, 0))

    ttk.Label(right, text="Transcribe in:").pack(anchor="w")
    app.v_active_language = tk.StringVar(value=languages_mod.label(cfg.whisper.language or "en"))
    app.active_language_box = ttk.Combobox(
        right, textvariable=app.v_active_language, state="readonly", width=24)
    app.active_language_box.pack(anchor="w", pady=(2, 8))

    ttk.Label(
        right,
        text=("Only one language is active at a time. The others stay configured "
              "and downloaded, so switching is instant."),
        foreground="#777", wraplength=220, justify="left",
    ).pack(anchor="w")

    sizes = "\n".join(
        f"  {name}: {size} — {note}"
        for name, (size, note) in languages_mod.SIZES.items()
    )
    ttk.Label(right, text=f"Download sizes:\n{sizes}", foreground="#777",
              justify="left", wraplength=220).pack(anchor="w", pady=(10, 0))

    app.v_language_status = tk.StringVar(value="")
    ttk.Label(frame, textvariable=app.v_language_status, foreground="#333",
              wraplength=880, justify="left").pack(fill="x", pady=(10, 0))

    buttons = footer
    app.language_save_button = ttk.Button(
        buttons, text="Save and download", command=lambda: _save_languages(app))
    app.language_save_button.pack(side="right")
    ttk.Button(buttons, text="Open model folder",
               command=lambda: _open_models(app)).pack(side="left")

    _refresh_languages(app)


# -- table ---------------------------------------------------------------


def _configured(app) -> dict[str, str]:
    return dict(app.store.config.whisper.languages or {"en": "small"})


def _refresh_languages(app, select: str | None = None) -> None:
    configured = _configured(app)

    app.language_tree.delete(*app.language_tree.get_children())
    for code in sorted(configured, key=lambda c: (c != "en", c)):
        size = configured[code]
        model = languages_mod.model_name(code, size)
        app.language_tree.insert(
            "", "end", iid=code,
            values=(languages_mod.label(code), size, model,
                    "yes" if _is_downloaded(app, model) else "no"),
        )

    labels = [languages_mod.label(code) for code in sorted(configured,
                                                           key=lambda c: (c != "en", c))]
    app.active_language_box.configure(values=labels)
    if select and languages_mod.label(select) in labels:
        app.v_active_language.set(languages_mod.label(select))
    elif app.v_active_language.get() not in labels and labels:
        app.v_active_language.set(labels[0])


def _is_downloaded(app, model: str) -> bool:
    """Whether the cache already holds this model.

    A heuristic on the cache layout, not a guarantee — it drives a hint in the
    table, and downloading again is cheap when it is already there.
    """
    root = paths.model_dir()
    if not root.exists():
        return False
    needle = model.replace(".", "").replace("-", "").lower()
    for child in root.rglob("*"):
        if child.is_dir() and needle in child.name.replace("-", "").replace(".", "").lower():
            return True
    return False


def _selected(app) -> str | None:
    selection = app.language_tree.selection()
    return selection[0] if selection else None


def _add_language(app) -> None:
    code = languages_mod.code_from_label(app.v_new_language.get())
    if code not in languages_mod.WHISPER_LANGUAGES:
        messagebox.showerror("PitRadio", f"{code!r} is not a Whisper language.")
        return

    configured = _configured(app)
    configured[code] = app.v_new_size.get()
    app.store.config.whisper.languages = configured
    _refresh_languages(app, select=code)
    app.v_language_status.set(
        f"{languages_mod.label(code)} set to {app.v_new_size.get()} — "
        f"press Save and download to fetch it."
    )


def _remove_language(app) -> None:
    code = _selected(app)
    if code is None:
        return
    configured = _configured(app)
    if len(configured) <= 1:
        messagebox.showinfo("PitRadio", "At least one language has to stay configured.")
        return
    configured.pop(code, None)
    app.store.config.whisper.languages = configured
    _refresh_languages(app)


def _open_models(app) -> None:
    from pitradio.ui import gui_settings

    gui_settings.open_folder(paths.model_dir())


# -- saving and downloading ----------------------------------------------


def _save_languages(app) -> None:
    """Write the config, then fetch every configured model in the background."""
    cfg = app.store.config
    active = languages_mod.code_from_label(app.v_active_language.get())
    configured = _configured(app)

    if active not in configured:
        messagebox.showerror(
            "PitRadio", "The active language must be one of the configured ones.")
        return

    cfg.whisper.languages = configured
    cfg.whisper.language = active
    # The worker only ever sees `model`; everything above is bookkeeping for
    # this tab.
    cfg.whisper.model = languages_mod.model_name(active, configured[active])
    app.save_config()

    if app.transcriber is None:
        app.v_language_status.set(
            "Saved. Model downloads aren't available in this mode.")
        return

    wanted = sorted(
        {languages_mod.model_name(code, size) for code, size in configured.items()}
    )
    app.language_save_button.state(["disabled"])
    app.v_language_status.set(f"Saved. Fetching {len(wanted)} model(s)…")

    def work() -> None:
        failures = []
        for index, model in enumerate(wanted, start=1):
            app.root.after(0, lambda m=model, i=index: app.v_language_status.set(
                f"Downloading {m} ({i} of {len(wanted)})… this can take a while."))
            error = speech.download_model(model, paths.model_dir(),
                                          app.store.config.whisper.compute_type)
            if error:
                failures.append(f"{model}: {error}")

        def done() -> None:
            app.language_save_button.state(["!disabled"])
            _refresh_languages(app)
            if failures:
                app.v_language_status.set("Some models failed:\n" + "\n".join(failures))
            else:
                app.v_language_status.set(
                    f"Ready. Transcribing in {languages_mod.language_name(active)} "
                    f"using {cfg.whisper.model}."
                )
                # The running transcriber still holds the previous model.
                app.reload_model()

        app.root.after(0, done)

    threading.Thread(target=work, name="model-download", daemon=True).start()
