"""The race engineer: a named voice that talks back.

What the pieces are, in the order they matter:

* [service.py](service.py) — the thread that watches the sim and decides what
  to say. The only part the rest of the app knows about.
* [routines.py](routines.py) — things it does until told to stop, and the
  corner coach that ships with it.
* [coaching.py](coaching.py) — lap traces, finding corners in them, comparing
  two laps through one. Pure.
* [lines.py](lines.py) — everything it says, in the driver's language.
* [phrases.py](phrases.py) — how a sentence becomes a command. Pure.
* [spotter.py](spotter.py) — who is alongside. Pure.
* [speaking.py](speaking.py) — the queue, and mixing pack takes with
  synthesised ones.
* [tts.py](tts.py) — Windows' own speech engine, out of process.
* [packs.py](packs.py) — recorded voice packs, laid out the way Crew Chief's
  are so a generated one can be dropped straight in.
* [personas.py](personas.py) — the four engineers that ship.

Nothing here imports `winapi`, which is what lets all of it be exercised — and
most of it tested — on a machine that is not the one it runs on.
"""

from pitradio.engineer.service import EngineerService

__all__ = ["EngineerService"]
