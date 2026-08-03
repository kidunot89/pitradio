# Screenshots

Generated, not hand-made:

```bash
python packaging/screenshots.py           # all of them
python packaging/screenshots.py trigger   # just one
```

Each is cropped to a **named widget** rather than to pixel coordinates, so a
shot stays correct after the layout moves. Run it on Windows — that is where
the app runs and what the README's readers will see, and ttk draws differently
on every platform.

On macOS the terminal needs Screen Recording permission. Without it
`ImageGrab` returns the desktop wallpaper rather than an error; the script
detects that and refuses to write the file.
