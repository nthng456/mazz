"""
Live terminal dashboard for the Biomaze downloader (Rich).

The live region holds exactly two fixed-height boxes:
  - a DOWNLOAD panel  (the one video currently downloading; cleared between)
  - an UPLOAD panel    (both upload workers + the waiting queue)

plus a single sticky line beneath them for the most recent warning/error.

There is deliberately NO scrolling log region. A live renderable that grows
taller than the window can't be redrawn in place — Rich stacks frames instead
of updating, which is what left a trail of duplicated boxes in the output.
Keeping the region to two short boxes of constant height (and cropping any
overflow) means the frame always redraws in place.

The relevant per-item results that used to scroll past are folded INTO the
boxes instead: each panel carries a dim "Recent" line set via dl_note()/
up_note() (last finished download / upload). Routine INFO chatter no longer
reaches the screen at all — it still lands in downloader.log via the file
handler. Only WARNING+ surfaces, as the sticky alert line.

Everything is driven from a single lock-guarded state object so the download
thread, the two upload workers, and Rich's own refresh thread never race.

When stdout is not a real, tall-enough TTY (redirected to a file, CI logs, a
short window) the whole live layer is skipped and logging falls back to plain
lines — see _can_render() / is_active().

Upload note: sync_bucket(quiet=True) exposes no byte-level progress callback,
so the upload panel shows an honest moving pulse + elapsed + file size rather
than a fabricated percentage. Downloads have a real segment count, so their
bar is a true percentage.
"""

import logging
import sys
import threading
import time

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.live import Live


# Colour per log level — muted so the panels stay the focal point.
_LEVEL_STYLE = {
    logging.DEBUG: "dim",
    logging.INFO: "white",
    logging.WARNING: "yellow",
    logging.ERROR: "bold red",
    logging.CRITICAL: "bold white on red",
}


class DashboardLogHandler(logging.Handler):
    """
    Surfaces only WARNING+ records as the dashboard's single sticky alert line.

    Routine INFO chatter is dropped here on purpose — the relevant results are
    folded into the panels via dl_note()/up_note(), and the full history is
    still written to downloader.log by the file handler.
    """

    def __init__(self, dashboard):
        super().__init__()
        self.dashboard = dashboard

    def emit(self, record):
        if record.levelno < logging.WARNING:
            return
        try:
            msg = self.format(record)
            style = _LEVEL_STYLE.get(record.levelno, "yellow")
            self.dashboard.set_alert(msg, style)
        except Exception:
            self.handleError(record)


def _bar(frac: float, width: int = 24,
         fill_style: str = "green", empty_style: str = "grey23") -> Text:
    """A determinate block progress bar as styled Text."""
    frac = 0.0 if frac < 0 else (1.0 if frac > 1 else frac)
    filled = int(width * frac)
    t = Text()
    t.append("█" * filled, style=fill_style)
    t.append("░" * (width - filled), style=empty_style)
    return t


def _pulse(elapsed: float, width: int = 24, block: int = 6,
           fill_style: str = "magenta", empty_style: str = "grey23") -> Text:
    """
    An indeterminate marquee bar for uploads (no real % is available).

    A `block`-wide lit window slides across and wraps; position is derived
    purely from elapsed seconds so Rich's own refresh animates it smoothly.
    """
    span = max(1, width)
    pos = int(elapsed * 12) % span
    cells = ["░"] * span
    for k in range(block):
        cells[(pos + k) % span] = "█"
    t = Text()
    run_style = None
    run = ""
    for c in cells:
        s = fill_style if c == "█" else empty_style
        if s != run_style:
            if run:
                t.append(run, style=run_style)
            run, run_style = c, s
        else:
            run += c
    if run:
        t.append(run, style=run_style)
    return t


class Dashboard:
    """
    Thread-safe live dashboard.  All public set_/push_ methods may be called
    from any thread; each takes _lock briefly and asks Live to re-render.
    """

    _REFRESH_HZ = 8      # Rich Live refresh rate (renders per second)
    _MIN_HEIGHT = 12     # need room for both panels; below this fall back to plain logs

    def __init__(self, total: int):
        self._total = total
        self._lock = threading.RLock()
        self._console = Console(highlight=False)

        # Most recent WARNING+ line, shown sticky beneath the panels ("" = none).
        self._alert = ("", "")

        # Download state (single active video)
        self._dl = {
            "status": "idle",   # idle | extracting | downloading | done
            "filename": "", "job": 0, "quality": "",
            "done": 0, "total": 0, "mb": 0.0,
            "speed": 0.0, "eta": 0.0, "elapsed": 0.0,
            "note": "",         # last finished download, shown dim in the panel
        }

        # Upload state (two workers) + queue
        self._workers = [self._idle_worker() for _ in range(2)]
        self._queue_names: list[str] = []
        self._up_note = ""      # last finished upload, shown dim in the panel

        self._live: Live | None = None

    @staticmethod
    def _idle_worker() -> dict:
        return {"status": "idle", "filename": "", "quality": "",
                "mb": 0.0, "start": 0.0}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def _can_render(self) -> bool:
        """
        Only drive a Live display on a real, overwrite-capable terminal.

        Rich's own is_terminal can report True for a redirected stream (e.g.
        when FORCE_COLOR is set), and a renderable taller than the window can't
        be redrawn in place — both make Live stack frames instead of updating,
        which is what jumbled the logs. Checking the fd and the height directly
        keeps those cases on the plain-logging path.
        """
        try:
            if not sys.stdout.isatty():
                return False
        except Exception:
            return False
        return (self._console.is_terminal
                and self._console.size.height >= self._MIN_HEIGHT)

    def start(self) -> "Dashboard":
        if self._can_render():
            self._live = Live(
                self._render(),
                console=self._console,
                refresh_per_second=self._REFRESH_HZ,
                transient=False,
                vertical_overflow="crop",
            )
            self._live.start()
        return self

    def stop(self) -> None:
        if self._live:
            self._live.update(self._render())
            self._live.stop()
            self._live = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()

    def is_active(self) -> bool:
        return self._live is not None

    def _refresh(self) -> None:
        if self._live:
            self._live.update(self._render())

    # ------------------------------------------------------------------ #
    # Alert (single sticky line for the most recent WARNING+)
    # ------------------------------------------------------------------ #

    def set_alert(self, msg: str, style: str = "yellow") -> None:
        # One line, rendered INSIDE the Live region beneath the panels, so it
        # redraws in place and never scrolls the screen (scrollback is what
        # re-pinned Live and stacked the panels). Full history still lands in
        # downloader.log via the file handler.
        with self._lock:
            self._alert = (msg, style)
        self._refresh()

    # ------------------------------------------------------------------ #
    # Download setters
    # ------------------------------------------------------------------ #

    def dl_extracting(self, filename: str, job: int) -> None:
        with self._lock:
            self._dl.update(status="extracting", filename=filename, job=job,
                            done=0, total=0, mb=0.0, speed=0.0, eta=0.0, elapsed=0.0)
        self._refresh()

    def dl_start(self, filename: str, job: int, quality: str) -> None:
        with self._lock:
            self._dl.update(status="downloading", filename=filename, job=job,
                            quality=quality, done=0, total=0, mb=0.0,
                            speed=0.0, eta=0.0, elapsed=0.0)
        self._refresh()

    def dl_progress(self, done: int, total: int, mb: float,
                    speed: float, eta: float, elapsed: float) -> None:
        with self._lock:
            self._dl.update(done=done, total=total, mb=mb,
                            speed=speed, eta=eta, elapsed=elapsed)
        self._refresh()

    def dl_idle(self) -> None:
        with self._lock:
            self._dl.update(status="idle", filename="")
        self._refresh()

    def dl_done_all(self) -> None:
        with self._lock:
            self._dl.update(status="done")
        self._refresh()

    def dl_note(self, msg: str) -> None:
        """Set the dim 'Recent' line in the DOWNLOAD panel (last finished/skipped)."""
        with self._lock:
            self._dl["note"] = msg
        self._refresh()

    # ------------------------------------------------------------------ #
    # Upload setters
    # ------------------------------------------------------------------ #

    def up_start(self, idx: int, filename: str, quality: str,
                 mb: float, start: float) -> None:
        with self._lock:
            if 0 <= idx < len(self._workers):
                self._workers[idx] = {"status": "uploading", "filename": filename,
                                      "quality": quality, "mb": mb, "start": start}
        self._refresh()

    def up_idle(self, idx: int) -> None:
        with self._lock:
            if 0 <= idx < len(self._workers):
                self._workers[idx] = self._idle_worker()
        self._refresh()

    def set_queue(self, names) -> None:
        with self._lock:
            self._queue_names = list(names)
        self._refresh()

    def up_note(self, msg: str) -> None:
        """Set the dim 'Recent' line in the UPLOAD panel (last finished upload)."""
        with self._lock:
            self._up_note = msg
        self._refresh()

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #

    def _render(self) -> Group:
        with self._lock:
            # Two fixed-height panels, plus one optional alert line. Nothing here
            # grows with the run, so the whole frame stays short enough for Rich
            # to redraw in place — the over-tall renderable is what stacked.
            parts = [self._render_download(), self._render_upload()]
            if self._alert[0]:
                parts.append(Text(self._alert[0], style=self._alert[1],
                                  overflow="ellipsis", no_wrap=True))
            return Group(*parts)

    def _render_download(self) -> Panel:
        d = self._dl
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold cyan", min_width=12, no_wrap=True)
        t.add_column(no_wrap=True, overflow="ellipsis")

        st = d["status"]
        if st == "idle":
            t.add_row("Status", Text("idle — waiting for next video", style="dim"))
        elif st == "extracting":
            t.add_row(f"[{d['job']}/{self._total}]",
                      Text(f"⟳ {d['filename']}  —  extracting m3u8…", style="yellow"))
        elif st == "downloading":
            frac = d["done"] / d["total"] if d["total"] else 0
            t.add_row(f"[{d['job']}/{self._total}]",
                      Text(f"▶ {d['filename']}  ({d['quality']}p)", style="bold white"))
            t.add_row("Progress", Text.assemble(
                _bar(frac, 28, "cyan"),
                Text(f"  {d['done']}/{d['total']} segs", style="cyan")))
            t.add_row("Size · Speed", Text(
                f"{d['mb']:.0f} MB   {d['speed']:.0f} MB/s   ETA {d['eta']:.0f}s",
                style="green"))
            t.add_row("Elapsed", Text(f"{d['elapsed']:.0f}s", style="dim"))
        elif st == "done":
            t.add_row("✓", Text(
                f"All {self._total} videos downloaded — draining uploads…",
                style="bold green"))

        if d["note"]:
            t.add_row("Recent", Text(d["note"], style="dim", overflow="ellipsis"))

        return Panel(t, title="[bold cyan]DOWNLOAD[/]",
                     border_style="cyan", padding=(0, 1))

    def _render_upload(self) -> Panel:
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold magenta", min_width=9, no_wrap=True)
        t.add_column(no_wrap=True, overflow="ellipsis")

        now = time.monotonic()
        for i, w in enumerate(self._workers):
            label = f"Worker {i + 1}"
            if w["status"] == "idle" or not w["filename"]:
                t.add_row(label, Text("idle", style="dim"))
            else:
                elapsed = max(0.0, now - w["start"])
                t.add_row(label, Text.assemble(
                    Text(f"{w['filename']} [{w['quality']}p]  ", style="white"),
                    _pulse(elapsed, 24),
                    Text(f"  {w['mb']:.0f} MB · {elapsed:.0f}s", style="magenta")))

        q = self._queue_names
        if q:
            shown = "  ·  ".join(q[:6]) + ("  …" if len(q) > 6 else "")
            t.add_row("Queue", Text(f"({len(q)})  {shown}", style="yellow"))
        else:
            t.add_row("Queue", Text("empty", style="dim"))

        if self._up_note:
            t.add_row("Recent", Text(self._up_note, style="dim", overflow="ellipsis"))

        return Panel(t, title="[bold magenta]UPLOAD[/]",
                     border_style="magenta", padding=(0, 1))


# --- Module-level singleton shared between the download thread and workers --- #
_dashboard: "Dashboard | None" = None


def get() -> "Dashboard | None":
    return _dashboard


def init(total: int) -> "Dashboard":
    global _dashboard
    _dashboard = Dashboard(total)
    return _dashboard
