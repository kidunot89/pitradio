"""The GUI's collaborators must actually provide what the GUI calls.

gui.py and gui_settings.py cannot import winapi, so they reach Windows-only
functionality through objects handed to App: hook, joystick, worker, recorder,
transcriber, checker. Nothing type-checks those calls, and every one of them
sits behind an `is None` guard that makes the failure invisible until a real
object is present on a real machine.

v0.1.4 shipped calling JoystickWatcher.list_devices(), which existed only as a
module function. The app crashed on startup for every user.

These tests read the ASTs rather than importing, so they run anywhere — which
matters, because joystick.py and hook.py import winapi and cannot be imported
off Windows at all.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
GUI_SOURCES = ["gui.py", "gui_settings.py"]

# Attribute on App -> module providing the object, and the class inside it.
COLLABORATORS = {
    "joystick": ("joystick.py", "JoystickWatcher"),
    "hook": ("hook.py", "KeyboardHook"),
    "worker": ("worker.py", "Worker"),
    "recorder": ("speech.py", "Recorder"),
    "transcriber": ("speech.py", "Transcriber"),
    "checker": ("updater.py", "UpdateChecker"),
    "plugins": ("plugins/__init__.py", "PluginRegistry"),
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

    Anchored to `app` and `self` deliberately. A looser match also catches
    `cfg.joystick.device`, which is the config *section* of the same name and
    has nothing to do with the watcher object.
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


def test_the_joystick_watcher_exposes_enumeration():
    """The GUI holds a watcher, not the module, so these must be on the class.

    Pinned separately because this is the exact regression that shipped: the
    functions existed at module level and the GUI could not reach them.
    """
    available = _class_attributes("joystick.py", "JoystickWatcher")
    assert {"list_devices", "describe"} <= available


def test_every_collaborator_is_covered():
    """A new optional collaborator must be added here, or it goes unchecked."""
    init = None
    for node in ast.walk(_tree("gui.py")):
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


TAB_SOURCES = ["gui_settings.py", "gui_language.py"]


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


def _save_buttons(node):
    """Button(...) calls whose text starts with 'Save'."""
    found = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        for kw in call.keywords:
            if (kw.arg == "text" and isinstance(kw.value, ast.Constant)
                    and str(kw.value.value).startswith("Save")):
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
