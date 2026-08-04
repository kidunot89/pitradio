"""PitRadio — push-to-talk voice dictation for sim racing.

The version lives here rather than in `__main__` so that packaging and the
updater can read it without importing the CLI, which pulls in tkinter and the
speech stack.
"""

__version__ = "0.1.23"
