"""The GUI's collaborators must actually provide what the GUI calls.

gui.py and gui_settings.py cannot import winapi, so they reach Windows-only
functionality through objects handed to App: hook, worker, recorder,
transcriber, checker. Nothing type-checks those calls, and every one of them
sits behind an `is None` guard that makes the failure invisible until a real
object is present on a real machine.

v0.1.4 shipped a GUI calling a method that existed only at module level. The
app crashed on startup for every user.

These tests read the ASTs rather than importing, so they run anywhere — which
matters, because hook.py imports winapi and cannot be imported off Windows at
all.
"""

import ast
import ctypes
import logging
import re
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
GUI_SOURCES = ["src/pitradio/ui/gui.py", "src/pitradio/ui/gui_settings.py"]

# Attribute on App -> module providing the object, and the class inside it.
COLLABORATORS = {
    "hook": ("src/pitradio/input/hook.py", "KeyboardHook"),
    "worker": ("src/pitradio/worker.py", "Worker"),
    "recorder": ("src/pitradio/speech.py", "Recorder"),
    "transcriber": ("src/pitradio/speech.py", "Transcriber"),
    "checker": ("src/pitradio/updater.py", "UpdateChecker"),
    "plugins": ("src/pitradio/plugins/__init__.py", "PluginRegistry"),
}


def _tree(filename: str) -> ast.Module:
    return ast.parse((ROOT / filename).read_text(encoding="utf-8"))


def _class_attributes(filename: str, class_name: str) -> set[str]:
    """Method and assigned-attribute names on a class, without importing it."""
    for node in ast.walk(_tree(filename)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            names = set()
            for item in ast.walk(node):
                if isinstance(item, ast.FunctionDef):
                    names.add(item.name)
                # self.x = ... assignments, so attributes count too.
                elif isinstance(item, ast.Attribute) and isinstance(
                    item.value, ast.Name
                ) and item.value.id == "self":
                    names.add(item.attr)
            return names
    raise AssertionError(f"class {class_name} not found in {filename}")


def _attributes_used_on(collaborator: str) -> set[str]:
    """Every `app.<collaborator>.NAME` / `self.<collaborator>.NAME` in the GUI.

    Anchored to `app` and `self` deliberately: a looser match would also pick
    up same-named attributes on unrelated objects, such as config sections.
    """
    used = set()
    for filename in GUI_SOURCES:
        for node in ast.walk(_tree(filename)):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == collaborator
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id in ("app", "self")
            ):
                used.add(node.attr)
    return used


@pytest.mark.parametrize("collaborator", sorted(COLLABORATORS))
def test_gui_only_calls_methods_that_exist(collaborator):
    filename, class_name = COLLABORATORS[collaborator]
    available = _class_attributes(filename, class_name)
    used = _attributes_used_on(collaborator)

    missing = used - available
    assert not missing, (
        f"gui code calls {collaborator}.{{{', '.join(sorted(missing))}}} but "
        f"{class_name} in {filename} provides none of those. This crashes on "
        f"startup on a real machine and is invisible when the object is None."
    )


def test_every_collaborator_is_covered():
    """A new optional collaborator must be added here, or it goes unchecked."""
    init = None
    for node in ast.walk(_tree("src/pitradio/ui/gui.py")):
        if isinstance(node, ast.ClassDef) and node.name == "App":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    init = item
    assert init is not None

    keyword_only = {arg.arg for arg in init.args.kwonlyargs}
    # use_tray is a flag, not an object with an interface.
    optional_objects = keyword_only - {"use_tray"}

    assert optional_objects <= set(COLLABORATORS), (
        f"App gained optional collaborators not covered here: "
        f"{sorted(optional_objects - set(COLLABORATORS))}"
    )


# -- editing tabs must scroll, with Save pinned --------------------------


TAB_SOURCES = ["src/pitradio/ui/gui_settings.py", "src/pitradio/ui/gui_language.py"]


def _tab_builders():
    """Every build_*_tab function, with its source module."""
    for name in TAB_SOURCES:
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("build_"):
                yield name, node


def _calls_named(node, names):
    return any(
        (isinstance(call.func, ast.Name) and call.func.id in names)
        or (isinstance(call.func, ast.Attribute) and call.func.attr in names)
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
    )


def _literal(node):
    """The string behind a node, seeing through a `t("…")` wrapper.

    Every label is translated now, so `text="Save"` became `text=t("Save")` —
    a Call rather than a Constant. Without this these checks found nothing and
    skipped themselves, which is the same as not having them.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "t" and node.args):
        return _literal(node.args[0])
    return None


def _save_buttons(node):
    """Button(...) calls whose text starts with 'Save'."""
    found = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        for kw in call.keywords:
            if kw.arg == "text" and (_literal(kw.value) or "").startswith("Save"):
                found.append(call)
    return found


@pytest.mark.parametrize("source,builder", [
    (source, node) for source, node in _tab_builders()
], ids=lambda v: v.name if isinstance(v, ast.FunctionDef) else v)
def test_a_tab_with_a_save_button_scrolls(source, builder):
    """Save must stay reachable however small the window is.

    Tab content outgrew the default window height and tkinter simply clips the
    overflow, so Save sat below the bottom edge with nothing to suggest it was
    there. Anyone adding a field to one of these tabs would reintroduce that
    without noticing, because it only shows up at small window sizes.
    """
    if not _save_buttons(builder):
        pytest.skip(f"{builder.name} has no Save button")

    assert _calls_named(builder, {"scrolling_tab", "scrolling_pane"}), (
        f"{source}:{builder.name} builds a Save button but does not use "
        f"scrolling_tab/scrolling_pane, so Save can end up off-screen"
    )


# -- a class must define the methods it calls on itself ------------------


# Everything with a thread body, where an AttributeError surfaces as the
# thread dying quietly rather than as a traceback anyone sees.
SELF_CALL_SOURCES = [
    "src/pitradio/worker.py",
    "src/pitradio/input/hook.py",
    "src/pitradio/updater.py",
    "src/pitradio/speech.py",
    "src/pitradio/state.py",
    "src/pitradio/ui/tray.py",
]


def _self_calls(class_node):
    """(name, line) for every self.foo(...) inside a class body."""
    calls = []
    for node in ast.walk(class_node):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"):
            calls.append((node.func.attr, node.lineno))
    return calls


def _defined_names(class_node):
    names = set()
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    # Attributes assigned anywhere in the class, e.g. in __init__.
    for node in ast.walk(class_node):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and isinstance(node.ctx, ast.Store)):
            names.add(node.attr)
    return names


# Bases whose inherited methods are legitimate self-calls. A class with any
# base outside this map is skipped rather than guessed at — a false failure
# here would be worse than the gap, because it would train people to ignore it.
KNOWN_BASES = {
    "Thread": threading.Thread,
    "threading.Thread": threading.Thread,
    "Handler": logging.Handler,
    "logging.Handler": logging.Handler,
    "Structure": ctypes.Structure,
    "ctypes.Structure": ctypes.Structure,
    "Union": ctypes.Union,
    "ctypes.Union": ctypes.Union,
}


def _base_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return None


def _inherited_names(class_node):
    """Names from base classes, or None if a base cannot be resolved."""
    names = set()
    for base in class_node.bases:
        name = _base_name(base)
        if name not in KNOWN_BASES:
            return None
        names |= set(dir(KNOWN_BASES[name]))
    return names


@pytest.mark.parametrize("source", SELF_CALL_SOURCES)
def test_every_self_call_resolves(source):
    """`self._foo()` where `_foo` does not exist is an AttributeError at runtime.

    Worker and hook methods run on their own threads and several are called
    from inside `except` blocks, so this fails as a thread quietly dying rather
    than as anything anyone sees. v0.1.22 shipped a call to a `_drop_pending`
    that was never written, on the error path of the pending-message handler.
    """
    tree = ast.parse((ROOT / source).read_text(encoding="utf-8"))
    missing = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        inherited = _inherited_names(node)
        if inherited is None:
            continue  # a base we cannot resolve; guessing would be worse
        known = _defined_names(node) | inherited | set(dir(object))
        for name, line in _self_calls(node):
            if name not in known:
                missing.append(f"{source}:{line} {node.name}.{name}()")

    assert not missing, "called but never defined: " + ", ".join(missing)


# -- portable modules must never reach winapi ----------------------------


# The modules that make --check-config and --gui-only work anywhere. Each must
# stay importable on a machine with no Win32 at all.
PORTABLE = [
    "src/pitradio/config.py",
    "src/pitradio/keys.py",
    "src/pitradio/paths.py",
    "src/pitradio/state.py",
    "src/pitradio/updater.py",
    "src/pitradio/languages.py",
    "src/pitradio/mentions.py",
    "src/pitradio/gestures.py",
    "src/pitradio/ui/gui.py",
    "src/pitradio/ui/gui_settings.py",
    "src/pitradio/ui/gui_language.py",
]


def _local_imports(source: str) -> set[str]:
    """Every module in this repo that `source` imports, at any nesting depth.

    Function-level imports count. `pitradio.py` defers its Windows imports into
    functions deliberately, but for these modules that only moves the crash
    from startup to whenever the function runs — which is worse, because it
    survives every check that merely imports the module.
    """
    tree = ast.parse((ROOT / source).read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return {name for name in found if (ROOT / f"{name}.py").exists()}


def _reaches_winapi(source: str, seen=None) -> list[str] | None:
    """The import chain from `source` to winapi, or None if there is none."""
    seen = seen or set()
    if source in seen:
        return None
    seen.add(source)

    for name in sorted(_local_imports(source)):
        if name == "winapi":
            return [source, "winapi"]
        chain = _reaches_winapi(f"{name}.py", seen)
        if chain is not None:
            return [source, *chain]
    return None


@pytest.mark.parametrize("source", PORTABLE)
def test_portable_modules_do_not_reach_winapi(source):
    """`--check-config` and `--gui-only` have to run on the development machine.

    This is the rule the whole GUI/Win32 split exists to enforce, and nothing
    was checking it: adding `import hook` inside one method of gui.py was
    enough to break `--gui-only` everywhere but Windows, and the module still
    imported fine so every test passed.
    """
    chain = _reaches_winapi(source)
    assert chain is None, "import chain reaches winapi: " + " -> ".join(chain)


# -- the tests must not assume a path separator --------------------------


def test_no_test_asserts_against_a_hardcoded_windows_path():
    """`Path("C:/app/x.exe")` renders as `C:\\app\\x.exe` on Windows.

    So an assertion written with forward slashes passes on Linux and macOS —
    where the whole suite normally runs — and fails on the one platform that
    ships. This has now cost three separate CI runs: once in test_build_flags
    on the Nuitka entry point, and twice in test_updater on the shim's paths.
    CLAUDE.md recorded it after the first.

    Build the expected string from the same Path instead of typing it.
    """
    import re

    offenders = []
    for path in sorted((ROOT / "tests").glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("assert"):
                continue
            # A drive letter followed by a forward slash, inside a literal.
            if re.search(r"""['"][A-Za-z]:/""", stripped):
                offenders.append(f"{path.name}:{number}")

    assert not offenders, (
        "these assertions hardcode a POSIX separator in a Windows path and "
        f"will fail on Windows: {offenders}"
    )


def test_nothing_reads_a_file_without_saying_which_encoding():
    """`read_text()` uses the locale codec, which is cp1252 on Windows.

    Every source file here is UTF-8 and most contain an em dash, so a read
    without an encoding works on Linux and macOS and raises UnicodeDecodeError
    on Windows. The guard directly above this one shipped with exactly that
    bug — a check against platform-dependent tests that was itself one.
    """
    import re

    offenders = []
    for folder in ("tests", "src", "packaging"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if re.search(r"\.read_text\(\s*\)", line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")

    assert not offenders, (
        "these read a file with the locale codec and will raise on Windows: "
        f"{offenders}"
    )


# -- colours belong to the palette, not to call sites ---------------------


COLOUR_OPTION = re.compile(
    r"\b(?:foreground|background|fg|bg|selectbackground|selectforeground|"
    r"insertbackground|highlightbackground|troughcolor)\s*=\s*[\"']#[0-9a-fA-F]{3,8}[\"']"
)


@pytest.mark.parametrize("filename", [
    "src/pitradio/ui/gui.py",
    "src/pitradio/ui/gui_settings.py",
    "src/pitradio/ui/gui_language.py",
    "src/pitradio/ui/tray.py",
])
def test_no_widget_hardcodes_a_colour(filename):
    """Every colour comes from the palette, so dark mode is not a guess.

    Eighteen labels used to carry a literal grey — `foreground="#666"` and
    friends. In light mode they looked deliberate. In dark mode `#333` on a
    `#16181d` window is very nearly the same colour, so the answer to every
    question on the Status tab was rendered almost invisible, and the whole
    window read as washed out.

    Nothing raises, nothing logs, and the tests all pass, because a colour is
    only wrong to look at. theme.py is exempt: it *is* the palette.
    """
    source = (ROOT / filename).read_text(encoding="utf-8")
    offenders = [
        f"line {i}: {line.strip()}"
        for i, line in enumerate(source.splitlines(), 1)
        if COLOUR_OPTION.search(line)
    ]
    assert not offenders, (
        f"{filename} hardcodes colours instead of using a ttk style:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse a style from theme.py (Value.TLabel, Hint.TLabel, "
          "Muted.TLabel, Heading.TLabel), or add one there."
    )
