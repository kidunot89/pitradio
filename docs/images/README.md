# Screenshots

Generated, not hand-made:

```bash
docker build -t pitradio-shots -f packaging/screenshots.Dockerfile packaging
docker run --rm -v "$PWD":/app -e PYTHONPATH=/app/src:/app/vendor pitradio-shots \
    xvfb-run -s "-screen 0 1500x1200x24" python packaging/screenshots.py
```

Or natively, if you have a display:

```bash
python packaging/screenshots.py           # all of them
python packaging/screenshots.py trigger   # just one
```

Each is cropped to a **named widget** rather than to pixel coordinates, so a
shot stays correct after the layout moves.

The container route needs no display and no permissions, which is why it is
first. The app forces ttk's `clam` theme and sets every colour explicitly, so
a Linux render matches Windows closely — the fonts differ, nothing else.

On macOS the terminal needs Screen Recording permission. Without it
`ImageGrab` returns the desktop wallpaper rather than an error; the script
detects that and refuses to write the file.
