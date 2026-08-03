"""ctypes bindings for the Win32 calls this app needs.

Importing this module fails anywhere but Windows, which is intentional — it
keeps the Windows-only surface in one place and lets the config layer and the
GUI stay importable on a development Mac.

Everything here declares argtypes and restype. Without them ctypes truncates
64-bit handles to int, which produces failures that look like "the API just
didn't work" rather than anything diagnosable.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
# Legacy multimedia joystick API. Chosen over SDL because it needs no extra
# dependency, and every native dependency this app has added has cost a release
# to get bundled correctly. It caps out at 32 buttons per device.
winmm = ctypes.WinDLL("winmm", use_last_error=True)

# Pointer-sized unsigned int. wintypes has no ULONG_PTR.
ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32
LRESULT = ctypes.c_ssize_t

# Tag on every synthetic event we send, so the hook can recognise its own
# output and pass it through instead of reacting to it.
INJECT_TAG = 0x50545244  # "PTRD"

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

MAPVK_VK_TO_VSC_EX = 4  # returns the extended-key prefix in the high byte

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenElevation = 20


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUTUNION(ctypes.Union):
    # MOUSEINPUT is the largest member; it has to be declared even though this
    # app only ever sends keyboard events, or INPUT comes out the wrong size
    # and SendInput rejects every call.
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


# -- joystick ------------------------------------------------------------

JOYERR_NOERROR = 0
JOY_RETURNBUTTONS = 0x00000080
MAX_JOYSTICK_BUTTONS = 32  # the API's ceiling, not ours
MAXPNAMELEN = 32
MAX_JOYSTICKOEMVXDNAME = 260


class JOYINFOEX(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("dwXpos", wintypes.DWORD),
        ("dwYpos", wintypes.DWORD),
        ("dwZpos", wintypes.DWORD),
        ("dwRpos", wintypes.DWORD),
        ("dwUpos", wintypes.DWORD),
        ("dwVpos", wintypes.DWORD),
        ("dwButtons", wintypes.DWORD),      # bitmask, one bit per button
        ("dwButtonNumber", wintypes.DWORD),
        ("dwPOV", wintypes.DWORD),
        ("dwReserved1", wintypes.DWORD),
        ("dwReserved2", wintypes.DWORD),
    ]


class JOYCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("szPname", wintypes.WCHAR * MAXPNAMELEN),
        ("wXmin", wintypes.UINT),
        ("wXmax", wintypes.UINT),
        ("wYmin", wintypes.UINT),
        ("wYmax", wintypes.UINT),
        ("wZmin", wintypes.UINT),
        ("wZmax", wintypes.UINT),
        ("wNumButtons", wintypes.UINT),
        ("wPeriodMin", wintypes.UINT),
        ("wPeriodMax", wintypes.UINT),
        ("wRmin", wintypes.UINT),
        ("wRmax", wintypes.UINT),
        ("wUmin", wintypes.UINT),
        ("wUmax", wintypes.UINT),
        ("wVmin", wintypes.UINT),
        ("wVmax", wintypes.UINT),
        ("wCaps", wintypes.UINT),
        ("wMaxAxes", wintypes.UINT),
        ("wNumAxes", wintypes.UINT),
        ("wMaxButtons", wintypes.UINT),
        ("szRegKey", wintypes.WCHAR * MAXPNAMELEN),
        ("szOEMVxD", wintypes.WCHAR * MAX_JOYSTICKOEMVXDNAME),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT

user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.MapVirtualKeyW.restype = wintypes.UINT

user32.VkKeyScanW.argtypes = (wintypes.WCHAR,)
user32.VkKeyScanW.restype = wintypes.SHORT

user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = wintypes.SHORT

winmm.joyGetNumDevs.restype = wintypes.UINT
winmm.joyGetDevCapsW.argtypes = (
    ctypes.c_void_p, ctypes.POINTER(JOYCAPSW), wintypes.UINT)
winmm.joyGetDevCapsW.restype = wintypes.UINT
winmm.joyGetPosEx.argtypes = (wintypes.UINT, ctypes.POINTER(JOYINFOEX))
winmm.joyGetPosEx.restype = wintypes.UINT

user32.SetWindowsHookExW.argtypes = (
    ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = wintypes.HHOOK

user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
user32.UnhookWindowsHookEx.restype = wintypes.BOOL

user32.CallNextHookEx.argtypes = (
    wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.restype = LRESULT

user32.GetMessageW.argtypes = (
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
user32.GetMessageW.restype = wintypes.BOOL

user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)

user32.PostThreadMessageW.argtypes = (
    wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.PostThreadMessageW.restype = wintypes.BOOL

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE

kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.GetCurrentProcess.restype = wintypes.HANDLE

advapi32.OpenProcessToken.argtypes = (
    wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
advapi32.OpenProcessToken.restype = wintypes.BOOL

advapi32.GetTokenInformation.argtypes = (
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD))
advapi32.GetTokenInformation.restype = wintypes.BOOL


def current_thread_id() -> int:
    return kernel32.GetCurrentThreadId()


def is_key_down(vk: int) -> bool:
    """Whether a key is physically held right now.

    Used to check a trigger's modifiers. The hook reports one key at a time, so
    a combo like ctrl+f12 can only be recognised by asking about the modifier
    separately when the main key arrives.
    """
    # High-order bit set means down. The low bit is a "pressed since last call"
    # toggle and must be ignored.
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def joystick_count() -> int:
    return winmm.joyGetNumDevs()


def joystick_name(device: int) -> str | None:
    """Product name of a joystick, or None if that ID has no device attached."""
    caps = JOYCAPSW()
    if winmm.joyGetDevCapsW(device, ctypes.byref(caps), ctypes.sizeof(caps)) != JOYERR_NOERROR:
        return None
    return caps.szPname or f"joystick {device}"


def joystick_buttons(device: int) -> int | None:
    caps = JOYCAPSW()
    if winmm.joyGetDevCapsW(device, ctypes.byref(caps), ctypes.sizeof(caps)) != JOYERR_NOERROR:
        return None
    return caps.wNumButtons


def joystick_button_mask(device: int) -> int | None:
    """Bitmask of currently held buttons, or None if the device isn't present.

    Only JOY_RETURNBUTTONS is requested: axes are polled every few milliseconds
    and we have no use for them.
    """
    info = JOYINFOEX()
    info.dwSize = ctypes.sizeof(JOYINFOEX)
    info.dwFlags = JOY_RETURNBUTTONS
    if winmm.joyGetPosEx(device, ctypes.byref(info)) != JOYERR_NOERROR:
        return None
    return info.dwButtons


def foreground_exe() -> str | None:
    """Lowercase executable name of the focused window, or None.

    PROCESS_QUERY_LIMITED_INFORMATION rather than PROCESS_QUERY_INFORMATION:
    the limited right is granted for processes we otherwise couldn't open,
    which matters because some sims and their launchers run elevated.
    """
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None

    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return buf.value.rsplit("\\", 1)[-1].lower()
    finally:
        kernel32.CloseHandle(handle)


def is_elevated() -> bool:
    """Whether this process runs with an elevated token.

    Worth knowing because SendInput into a higher-integrity process fails
    silently under UIPI — no exception, no error code, just nothing typed.
    """
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        return False
    try:
        elevation = TOKEN_ELEVATION()
        returned = wintypes.DWORD()
        ok = advapi32.GetTokenInformation(
            token,
            TokenElevation,
            ctypes.byref(elevation),
            ctypes.sizeof(elevation),
            ctypes.byref(returned),
        )
        return bool(ok and elevation.TokenIsElevated)
    finally:
        kernel32.CloseHandle(token)
