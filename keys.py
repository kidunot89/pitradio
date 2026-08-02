"""Key-name parsing and virtual-key constants.

Pure data and pure logic, deliberately: config validation has to work on a
machine with no Win32 (development happens on macOS), so the name table lives
here rather than in `inject`. `inject` turns these virtual-key codes into scan
codes at injection time via MapVirtualKeyW, which is the part that genuinely
needs Windows.

Key specs are strings like "enter", "f13", "ctrl+enter", "shift+t". Modifiers
are held down around the main key and released in reverse order.
"""

from __future__ import annotations

VK: dict[str, int] = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "pause": 0x13,
    "capslock": 0x14,
    "escape": 0x1B,
    "esc": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "printscreen": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "numlock": 0x90,
    "scrolllock": 0x91,
    # Punctuation, US layout. MapVirtualKeyW resolves these to the right scan
    # code for whatever layout is actually active.
    "semicolon": 0xBA,
    "plus": 0xBB,
    "comma": 0xBC,
    "minus": 0xBD,
    "period": 0xBE,
    "slash": 0xBF,
    "backtick": 0xC0,
    "grave": 0xC0,
    "leftbracket": 0xDB,
    "backslash": 0xDC,
    "rightbracket": 0xDD,
    "quote": 0xDE,
}

for _i in range(10):
    VK[str(_i)] = 0x30 + _i
    VK[f"numpad{_i}"] = 0x60 + _i

for _i in range(26):
    VK[chr(ord("a") + _i)] = 0x41 + _i

# F1..F24. F13-F24 are the useful ones here: no sim binds them, which is why
# the default trigger is F13.
for _i in range(1, 25):
    VK[f"f{_i}"] = 0x6F + _i

MODIFIERS: dict[str, int] = {
    "shift": 0xA0,   # left shift specifically; games read either
    "ctrl": 0xA2,
    "control": 0xA2,
    "alt": 0xA4,
    "lshift": 0xA0,
    "rshift": 0xA1,
    "lctrl": 0xA2,
    "rctrl": 0xA3,
    "lalt": 0xA4,
    "ralt": 0xA5,
}

VK.update(MODIFIERS)

# Keys that need KEYEVENTF_EXTENDEDKEY. Sending one of these without the flag
# produces the numpad twin instead, or nothing at all — and it fails silently,
# which is the worst kind of wrong here.
EXTENDED: frozenset[int] = frozenset(
    {
        0x21,  # page up
        0x22,  # page down
        0x23,  # end
        0x24,  # home
        0x25,  # left
        0x26,  # up
        0x27,  # right
        0x28,  # down
        0x2C,  # print screen
        0x2D,  # insert
        0x2E,  # delete
        0x90,  # num lock
        0xA3,  # right ctrl
        0xA5,  # right alt
    }
)


class KeyNameError(ValueError):
    """Raised for a key spec that has no chance of working."""


def parse_combo(spec: str) -> tuple[list[int], int]:
    """"ctrl+enter" -> ([0xA2], 0x0D). Raises KeyNameError on anything unknown."""
    if not isinstance(spec, str) or not spec.strip():
        raise KeyNameError("empty key")

    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise KeyNameError(f"unusable key {spec!r}")

    *mods, main = parts

    bad_mods = [m for m in mods if m not in MODIFIERS]
    if bad_mods:
        raise KeyNameError(f"unknown modifier(s) {', '.join(bad_mods)} in {spec!r}")

    if main not in VK:
        raise KeyNameError(f"unknown key {main!r} in {spec!r}")

    return [MODIFIERS[m] for m in mods], VK[main]


def parse_key(spec: str) -> int:
    """Single key, no modifiers — used for the trigger key."""
    mods, vk = parse_combo(spec)
    if mods:
        raise KeyNameError(
            f"trigger key {spec!r} cannot have modifiers: the low-level hook "
            f"sees one key at a time"
        )
    return vk


def name_for(vk: int) -> str:
    """Best-effort reverse lookup, for log lines."""
    for name, code in VK.items():
        if code == vk and len(name) > 1:
            return name
    for name, code in VK.items():
        if code == vk:
            return name
    return f"vk_{vk:#04x}"
