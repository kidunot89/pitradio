"""Opening a sim's shared memory block, without creating one.

Extracted so every sim plugin gets the same correct behaviour, because the
mistake here is silent and has already been made once.

**Never `mmap.mmap(fileno=0, tagname=...)`.** On Windows that calls
CreateFileMapping, which *creates* the block when it is absent. With the game
closed that fabricates a page-file-backed block of zeros under the game's own
name — so the plugin reports itself connected to a session that does not exist,
hands back a grid of nameless cars, and leaves a phantom mapping behind for
whatever starts next.

`OpenFileMappingW` only ever opens. It fails when the game is not running,
which is the answer actually wanted.
"""

from __future__ import annotations

import ctypes
import logging

log = logging.getLogger(__name__)

#: Read-only view of an existing mapping.
FILE_MAP_READ = 0x0004


def open_existing(name: str, size: int):
    """(handle, view) for a block somebody else published, or None."""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenFileMappingW.argtypes = (
        wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.OpenFileMappingW.restype = wintypes.HANDLE
    kernel32.MapViewOfFile.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_size_t)
    kernel32.MapViewOfFile.restype = ctypes.c_void_p

    handle = kernel32.OpenFileMappingW(FILE_MAP_READ, False, name)
    if not handle:
        return None

    view = kernel32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, size)
    if not view:
        kernel32.CloseHandle(handle)
        return None
    return handle, view


def close(handle, view) -> None:
    """Release a mapping. Never raises; a leak here costs a handle."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if view:
            kernel32.UnmapViewOfFile(ctypes.c_void_p(view))
        if handle:
            kernel32.CloseHandle(handle)
    except Exception:
        log.debug("releasing a shared memory mapping failed", exc_info=True)


def read(view, size: int) -> bytes:
    """A snapshot of the mapping as bytes.

    Copied rather than viewed, and that is the point: the sim is writing into
    this block while we read it, so parsing straight from the mapping can take
    a car's lap distance from one frame and its speed from the next. One
    `memmove` of a megabyte is far cheaper than a class of bug that produces
    plausible numbers describing a moment that never happened.
    """
    if not view or size <= 0:
        return b""
    buffer = (ctypes.c_char * size)()
    ctypes.memmove(buffer, ctypes.c_void_p(view), size)
    return buffer.raw
