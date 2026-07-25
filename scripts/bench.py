#!/usr/bin/env python3
"""Score how efficiently the bar paints itself.

    python3 scripts/bench.py                 # the score and where it comes from
    python3 scripts/bench.py --scenarios     # per-scenario detail
    python3 scripts/bench.py --json          # machine-readable, for tracking
    python3 scripts/bench.py --save          # write scripts/bench-baseline.json
    python3 scripts/bench.py --check         # fail if the paint metrics regressed

A terminal UI has two costs, and only one of them is CPU. The other is the
bytes it pushes down the pipe: over ssh, or through a multiplexer, a repaint
that could have been forty bytes and was four kilobytes is felt as lag no
profiler will show you. So the bench drives the real `Bar` against fixture
data through a headless terminal, models the screen the way a terminal would,
and asks of every frame: how many bytes did this actually need?

Five things are scored, weighted by how much each one is felt:

* paint efficiency -- emitted bytes against the bytes a diffing renderer
  would have sent for the same visual change. The single biggest lever.
* frame discipline -- the share of frames that changed anything at all.
  A frame that repaints an identical screen is pure cost.
* input latency -- keystroke to composed frame, p99.
* scale -- the same latency with 200 rows in the list, because scoring runs
  over every item on every keystroke.
* blocking i/o -- calls to Herdr per second on the input thread, each of
  which can stall typing for as long as the server takes to answer.

The paint and frame numbers are deterministic: same code, same score, on any
machine. The latency numbers are wall-clock and will wobble, which is why
--check ignores them by default.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from herdr_bar import app as app_module  # noqa: E402
from herdr_bar.app import PREVIEW_DEBOUNCE, SPINNER_INTERVAL, Bar  # noqa: E402
from herdr_bar.config import Config  # noqa: E402
from herdr_bar.mru import Recents  # noqa: E402
from herdr_bar.textutil import char_width  # noqa: E402
from herdr_bar.theme import Theme  # noqa: E402
from tests.fixtures import PANE_AGES, PREVIEW_TEXT, snapshot  # noqa: E402

BASELINE_PATH = os.path.join(ROOT, "scripts", "bench-baseline.json")


# -- the screen ------------------------------------------------------------
#
# Enough of a terminal to answer "what is on screen now, and what would it
# have cost to get here". The bar draws with absolute cursor addressing, SGR
# colors, and the two erase sequences; anything outside that alphabet is
# counted as unhandled so the bench can say when it has stopped being honest.


class Style(tuple):
    """Rendition of one cell: (fg, bg, bold, underline, reverse)."""

    __slots__ = ()

    @property
    def fg(self) -> Optional[str]:
        return self[0]

    @property
    def bg(self) -> Optional[str]:
        return self[1]


DEFAULT_STYLE = Style((None, None, False, False, False))
BLANK = (" ", DEFAULT_STYLE)


def _sgr(params: str, style: Style) -> Style:
    """Apply one SGR sequence's parameters to a style."""
    fg, bg, bold, underline, reverse = style
    parts = params.split(";") if params else ["0"]
    index = 0
    while index < len(parts):
        part = parts[index] or "0"
        if part in ("38", "48"):
            # Extended color: 5;n for the 256 palette, 2;r;g;b for truecolor.
            mode = parts[index + 1] if index + 1 < len(parts) else ""
            length = 3 if mode == "2" else 1
            value = ";".join(parts[index : index + 2 + length])
            if part == "38":
                fg = value
            else:
                bg = value
            index += 2 + length
            continue
        if part == "0":
            fg = bg = None
            bold = underline = reverse = False
        elif part == "1":
            bold = True
        elif part == "4":
            underline = True
        elif part == "7":
            reverse = True
        elif part == "22":
            bold = False
        elif part == "24":
            underline = False
        elif part == "27":
            reverse = False
        elif part == "39":
            fg = None
        elif part == "49":
            bg = None
        index += 1
    return Style((fg, bg, bold, underline, reverse))


def encode_style(current: Style, target: Style) -> str:
    """The shortest SGR that moves a terminal from one rendition to another.

    Turning an attribute off costs a reset and a rebuild, so the additive
    case -- which is most of them -- stays cheap.
    """
    if current == target:
        return ""
    turning_off = (
        (current[2] and not target[2])
        or (current[3] and not target[3])
        or (current[4] and not target[4])
        or (current[0] is not None and target[0] != current[0])
        or (current[1] is not None and target[1] != current[1])
    )
    out: List[str] = []
    if turning_off:
        if target == DEFAULT_STYLE:
            return "\x1b[0m"
        out.append("0")
        base = DEFAULT_STYLE
    else:
        base = current
    if target[2] and not base[2]:
        out.append("1")
    if target[3] and not base[3]:
        out.append("4")
    if target[4] and not base[4]:
        out.append("7")
    if target[0] and target[0] != base[0]:
        out.append(target[0])
    if target[1] and target[1] != base[1]:
        out.append(target[1])
    return "\x1b[" + ";".join(out) + "m" if out else ""


class Screen(object):
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.cells = [[BLANK] * width for _ in range(height)]
        self.x = 0
        self.y = 0
        self.style = DEFAULT_STYLE
        self.unhandled = 0

    def snapshot_rows(self) -> List[Tuple]:
        return [tuple(row) for row in self.cells]

    def _clear(self, y: int, start: int, stop: int) -> None:
        row = self.cells[y]
        for x in range(max(0, start), min(self.width, stop)):
            row[x] = BLANK

    def _put(self, char: str) -> None:
        width = char_width(char)
        if width == 0 or self.y >= self.height:
            return
        if self.x >= self.width:
            return
        self.cells[self.y][self.x] = (char, self.style)
        if width == 2 and self.x + 1 < self.width:
            # The trailing half of a wide glyph holds no character of its
            # own, but it is still covered -- and still has to compare equal.
            self.cells[self.y][self.x + 1] = ("", self.style)
        self.x += width

    def feed(self, data: str) -> None:
        index = 0
        length = len(data)
        while index < length:
            char = data[index]
            if char != "\x1b":
                if char == "\n":
                    self.y += 1
                    self.x = 0
                elif char == "\r":
                    self.x = 0
                else:
                    self._put(char)
                index += 1
                continue
            if index + 1 >= length:
                break
            introducer = data[index + 1]
            if introducer == "[":
                end = index + 2
                while end < length and not ("\x40" <= data[end] <= "\x7e"):
                    end += 1
                if end >= length:
                    break
                self._csi(data[index + 2 : end], data[end])
                index = end + 1
                continue
            if introducer == "]":
                end = data.find("\x07", index)
                index = length if end < 0 else end + 1
                continue
            self.unhandled += 1
            index += 2

    def _csi(self, params: str, final: str) -> None:
        numbers = [int(part) if part.isdigit() else 0 for part in params.split(";")]
        if final == "m":
            self.style = _sgr(params, self.style)
        elif final in ("H", "f"):
            self.y = max(0, (numbers[0] if numbers and numbers[0] else 1) - 1)
            self.x = max(0, (numbers[1] if len(numbers) > 1 and numbers[1] else 1) - 1)
        elif final == "K":
            mode = numbers[0] if numbers else 0
            if self.y < self.height:
                if mode == 0:
                    self._clear(self.y, self.x, self.width)
                elif mode == 1:
                    self._clear(self.y, 0, self.x + 1)
                else:
                    self._clear(self.y, 0, self.width)
        elif final == "J":
            mode = numbers[0] if numbers else 0
            if mode == 0:
                self._clear(self.y, self.x, self.width)
                for y in range(self.y + 1, self.height):
                    self._clear(y, 0, self.width)
            elif mode == 1:
                for y in range(0, self.y):
                    self._clear(y, 0, self.width)
                self._clear(self.y, 0, self.x + 1)
            else:
                for y in range(self.height):
                    self._clear(y, 0, self.width)
        elif final in ("A", "B", "C", "D", "G", "h", "l", "n", "s", "u", "r", "t"):
            pass  # movement and modes: irrelevant to what ends up on screen
        else:
            self.unhandled += 1


def repaint(before: Sequence[Tuple], after: Sequence[Tuple], width: int) -> str:
    """What a diffing renderer would have emitted for this change.

    Rewrites only the span of each row that moved, jumping the cursor to it,
    and clears to end of line when the tail went blank. This is a model, not
    a promise -- but it is the same model every real damage-tracking TUI
    implements, so the ratio against it is a fair target.
    """
    out: List[str] = []
    # The rendition the terminal is left in by the previous frame is not ours
    # to assume, so the first thing any update says is "start from nothing".
    style = DEFAULT_STYLE
    leading = "\x1b[0m"
    for y, (old, new) in enumerate(zip(before, after)):
        if old == new:
            continue
        first = 0
        while first < width and old[first] == new[first]:
            first += 1
        last = width - 1
        while last > first and old[last] == new[last]:
            last -= 1
        while first > 0 and new[first][0] == "":
            first -= 1  # never start mid-glyph

        tail_blank = all(cell == BLANK for cell in new[first:])
        span = list(range(first, last + 1))
        if tail_blank:
            span = []

        chunk: List[str] = ["\x1b[%d;%dH" % (y + 1, first + 1)]
        for x in span:
            char, cell_style = new[x]
            if char == "":
                continue
            move = encode_style(style, cell_style)
            if move:
                chunk.append(move)
                style = cell_style
            chunk.append(char)
        if tail_blank:
            if style != DEFAULT_STYLE:
                chunk.append("\x1b[0m")
                style = DEFAULT_STYLE
            chunk.append("\x1b[K")
        out.append("".join(chunk))
    return leading + "".join(out) if out else ""


# -- the harness -----------------------------------------------------------


class CountingClient(object):
    """The demo's fake Herdr, plus a tally of what the bar asked it for."""

    socket_path = None
    bin_path = "herdr"
    socket_ok = False

    def __init__(self, data: Optional[Dict[str, Any]] = None) -> None:
        self._snapshot = data or snapshot()
        self.calls = 0

    def snapshot(self) -> Dict[str, Any]:
        self.calls += 1
        return self._snapshot

    def read_pane(self, pane_id: str, lines: int, source: str = "visible") -> str:
        self.calls += 1
        return PREVIEW_TEXT

    def pane_age(self, pane_id: str) -> Optional[float]:
        self.calls += 1
        return PANE_AGES.get(pane_id, 3600.0)

    def close_tab(self, tab_id: str) -> None:
        self.calls += 1

    def focus_workspace(self, workspace_id: str) -> None:
        self.calls += 1

    def focus_tab(self, tab_id: str) -> None:
        self.calls += 1

    def focus_agent(self, target: str) -> None:
        self.calls += 1


class Recorder(object):
    """A terminal that keeps every frame, and prices them once it is over.

    Nothing is modelled while the bar is drawing: the screen replay is the
    expensive part of the bench, and it has no business inside a measurement
    of how long the bar took.
    """

    def __init__(self, width: int, height: int) -> None:
        self._size = (width, height)
        self.buffer: List[str] = []
        self.frames: List[str] = []
        self.counted_from = 0

    def size(self) -> Tuple[int, int]:
        return self._size

    def write(self, text: str) -> None:
        self.buffer.append(text)

    def flush(self) -> None:
        data = "".join(self.buffer)
        self.buffer = []
        if data:
            self.frames.append(data)

    def mark(self) -> None:
        """Stop counting what came before, but keep it: those frames are what
        put the screen into the state the measured ones start from."""
        self.counted_from = len(self.frames)

    def price(self) -> Dict[str, int]:
        """Replay every frame, counting what was sent against what changed."""
        width, height = self._size
        screen = Screen(width, height)
        emitted = needed = changed = counted = 0
        for index, data in enumerate(self.frames):
            before = screen.snapshot_rows()
            screen.feed(data)
            if index < self.counted_from:
                continue
            after = screen.snapshot_rows()
            counted += 1
            emitted += len(data.encode("utf-8"))
            if before != after:
                changed += 1
                needed += len(repaint(before, after, width).encode("utf-8"))
        return {
            "frames": counted,
            "changed": changed,
            "emitted": emitted,
            "needed": needed,
            "unhandled": screen.unhandled,
        }


class Clock(object):
    """A stand-in for `time` that only moves when a scenario says so.

    Every scenario runs on one. Partly so the idle scenario can cover four
    seconds instantly, but mostly so the bench is repeatable: the bar renders
    running times, and against a real clock a row reading "59s" on one run and
    "1m" on the next would move the byte counts for no reason at all.
    """

    def __init__(self, start: float = 10000.0) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class virtual_clock(object):
    def __enter__(self) -> Clock:
        self.clock = Clock()
        self._original = app_module.time
        app_module.time = self.clock  # type: ignore[assignment]
        return self.clock

    def __exit__(self, *exc_info) -> None:
        app_module.time = self._original  # type: ignore[assignment]


def build_bar(client: CountingClient, preview: Any = "auto") -> Bar:
    bar = Bar(
        client,
        Config({"selection_background": "237", "preview": preview}),
        Recents(None),
        Theme(selection_background="237"),
    )
    bar.bootstrap()
    return bar


def settle(bar: Bar, terminal: Recorder) -> None:
    """Reach the steady state the user actually looks at.

    The first frames of any session are spent filling in preview text and
    running times; measuring those as if they were typical would flatter a
    renderer that is slow in the steady state and punish one that is not.
    """
    bar.draw(terminal)
    if bar._preview_pending:
        bar._preview_pending = (bar._preview_pending[0], 0.0)
    bar.pump_preview(bar._list_height)
    while bar._age_queue:
        bar.pump_ages()
    bar.draw(terminal)
    terminal.mark()


class measure(object):
    """Time a whole keystroke: the state change and the frame it produces.

    Filtering happens on the way in, not at draw time, so timing the draw
    alone would miss the part that grows with the size of the session --
    which is the part worth watching.
    """

    def __init__(self, bar: Bar, terminal: Recorder, samples: List[float]) -> None:
        self.bar = bar
        self.terminal = terminal
        self.samples = samples

    def __enter__(self) -> "measure":
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc_info) -> None:
        self.bar.draw(self.terminal)
        self.samples.append((time.perf_counter() - self.start) * 1000.0)


def bulk_snapshot(extra: int) -> Dict[str, Any]:
    """The fixture session with `extra` more agent tabs bolted on."""
    data = snapshot()
    for index in range(extra):
        tab_id = "w1:t%d" % (index + 100)
        pane_id = "w1:p%d" % (index + 100)
        status = "working" if index % 3 else "idle"
        title = "session %d refactor pass" % index
        data["tabs"].append(
            {
                "tab_id": tab_id, "workspace_id": "w1", "label": title,
                "number": index + 100, "agent_status": status,
                "pane_count": 1, "focused": False,
            }
        )
        agent = {
            "pane_id": pane_id, "tab_id": tab_id, "workspace_id": "w1",
            "agent": "claude", "agent_status": status,
            "cwd": "/Users/dev/workspace/pacebeats/module%d" % index,
            "terminal_title_stripped": title, "focused": False,
        }
        data["agents"].append(agent)
        data["panes"].append(dict(agent))
    return data


# -- scenarios -------------------------------------------------------------
#
# Each returns the counters for one shape of use. `interactive` marks the
# ones whose draw timings stand for keystroke latency.


class Result(dict):
    pass


def _result(name: str, terminal: Recorder, samples: List[float], client: CountingClient,
            seconds: float, interactive: bool) -> Result:
    priced = Result(terminal.price())
    priced.update(
        name=name,
        calls=client.calls,
        seconds=seconds,
        samples=samples,
        interactive=interactive,
    )
    return priced


def scenario_open(clock: Clock) -> Result:
    """Cold open: what lands on screen the moment the chord is pressed."""
    client = CountingClient()
    terminal = Recorder(104, 22)
    samples: List[float] = []
    bar = build_bar(client)
    client.calls = 0
    with measure(bar, terminal, samples):
        pass
    return _result("open", terminal, samples, client, 0.0, False)


def scenario_typing(clock: Clock) -> Result:
    client = CountingClient()
    terminal = Recorder(104, 22)
    bar = build_bar(client)
    settle(bar, terminal)
    client.calls = 0
    samples: List[float] = []
    for char in "weekly":
        with measure(bar, terminal, samples):
            bar.insert(char)
    for _ in range(len("weekly")):
        with measure(bar, terminal, samples):
            bar.backspace()
    return _result("typing", terminal, samples, client, 0.0, True)


def scenario_navigate(clock: Clock) -> Result:
    """Walking the list, pausing long enough on each row to load its preview.

    The pause matters. Arrowing past a row costs one repaint; resting on it
    costs a second one when the debounce fires and the preview column fills
    in, and that is the pair of frames a real user generates.
    """
    client = CountingClient()
    terminal = Recorder(104, 22)
    bar = build_bar(client)
    settle(bar, terminal)
    client.calls = 0
    samples: List[float] = []
    for step in range(14):
        with measure(bar, terminal, samples):
            bar.handle_key("down" if step % 5 != 4 else "up")
        clock.advance(PREVIEW_DEBOUNCE)
        bar.pump_preview(bar._list_height)
        if bar._dirty:
            bar.draw(terminal)
    return _result("navigate", terminal, samples, client, 0.0, True)


def scenario_scope(clock: Clock) -> Result:
    client = CountingClient()
    terminal = Recorder(104, 22)
    bar = build_bar(client)
    settle(bar, terminal)
    client.calls = 0
    samples: List[float] = []
    for _ in range(8):
        with measure(bar, terminal, samples):
            bar.handle_key("tab")
    return _result("scope", terminal, samples, client, 0.0, True)


def scenario_narrow(clock: Clock) -> Result:
    """A popup too tight for the preview: the layout the fallbacks produce."""
    client = CountingClient()
    terminal = Recorder(72, 14)
    bar = build_bar(client)
    settle(bar, terminal)
    client.calls = 0
    samples: List[float] = []
    for char in "retry":
        with measure(bar, terminal, samples):
            bar.insert(char)
    return _result("narrow", terminal, samples, client, 0.0, True)


def scenario_scale(clock: Clock) -> Result:
    """200 rows: scoring walks every item and every field on every keystroke."""
    client = CountingClient(bulk_snapshot(200))
    terminal = Recorder(104, 22)
    bar = build_bar(client)
    settle(bar, terminal)
    client.calls = 0
    samples: List[float] = []
    for char in "session":
        with measure(bar, terminal, samples):
            bar.insert(char)
    return _result("scale", terminal, samples, client, 0.0, True)


def scenario_idle(clock: Clock, seconds: float = 4.0) -> Result:
    """The bar sitting open while an agent works.

    Replays the main loop's timers against a virtual clock: the spinner wakes
    it every 110ms, the refresh every 900ms, and each wake decides for itself
    whether anything needs redrawing.
    """
    client = CountingClient()
    terminal = Recorder(104, 22)
    bar = build_bar(client)
    settle(bar, terminal)
    bar._last_refresh = clock.monotonic()
    bar._last_spin = clock.monotonic()
    client.calls = 0
    samples: List[float] = []
    end = clock.monotonic() + seconds
    while clock.monotonic() < end:
        clock.advance(bar._next_timeout(None))
        now = clock.monotonic()
        if now - bar._last_refresh >= bar.config.refresh_ms / 1000.0:
            bar.refresh()
        if bar.config.spinner and now - bar._last_spin >= SPINNER_INTERVAL:
            bar._last_spin = now
            if any(row.item.status == "working" for row in bar.rows):
                bar.tick += 1
                bar._dirty = True
        bar.pump_preview(bar._list_height)
        bar.pump_ages()
        if bar.status and now >= bar.status_until:
            bar.status = None
            bar._dirty = True
        if bar._dirty:
            with measure(bar, terminal, samples):
                pass
    return _result("idle", terminal, samples, client, seconds, False)


SCENARIOS = (
    scenario_open,
    scenario_typing,
    scenario_navigate,
    scenario_scope,
    scenario_narrow,
    scenario_scale,
    scenario_idle,
)


def run(scenario) -> Result:
    """One scenario, on a clock that only moves when the scenario says so."""
    with virtual_clock() as clock:
        return scenario(clock)


def run_all() -> List[Result]:
    return [run(scenario) for scenario in SCENARIOS]


# -- scoring ---------------------------------------------------------------
#
# Budgets are set from what is achievable and what is felt, not from where
# the code happens to sit today -- a score tuned to flatter its own baseline
# tells you nothing about whether a change helped.

LATENCY_GOOD_MS = 4.0    # comfortably inside one 60Hz frame, on a busy machine
LATENCY_BAD_MS = 16.0    # a whole frame spent on one keystroke: visible
SCALE_BAD_MS = 24.0      # 200 rows is a big session; a little more slack
IO_GOOD_PER_S = 1.2      # the 900ms refresh, and nothing else
IO_BAD_PER_S = 10.0

WEIGHTS = (
    ("paint", 35, "paint efficiency"),
    ("frames", 15, "frame discipline"),
    ("latency", 25, "input latency"),
    ("scale", 15, "scale (200 rows)"),
    ("io", 10, "blocking i/o"),
)


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _curve(value: float, good: float, bad: float) -> float:
    """100 at or under `good`, 0 at or over `bad`, straight line between."""
    if value <= good:
        return 100.0
    if value >= bad:
        return 0.0
    return 100.0 * (bad - value) / (bad - good)


def score(results: Sequence[Result]) -> Dict[str, Any]:
    by_name = {result["name"]: result for result in results}
    emitted = sum(r["emitted"] for r in results)
    needed = sum(r["needed"] for r in results)
    frames = sum(r["frames"] for r in results)
    changed = sum(r["changed"] for r in results)

    interactive = [ms for r in results if r["interactive"] and r["name"] != "scale"
                   for ms in r["samples"]]
    scale_samples = by_name["scale"]["samples"]
    idle = by_name["idle"]
    calls_per_second = idle["calls"] / idle["seconds"] if idle["seconds"] else 0.0

    paint_ratio = (needed / emitted) if emitted else 1.0
    frame_ratio = (changed / frames) if frames else 1.0
    latency_p99 = percentile(interactive, 0.99)
    scale_p99 = percentile(scale_samples, 0.99)

    parts = {
        "paint": 100.0 * min(1.0, paint_ratio),
        "frames": 100.0 * frame_ratio,
        "latency": _curve(latency_p99, LATENCY_GOOD_MS, LATENCY_BAD_MS),
        "scale": _curve(scale_p99, LATENCY_GOOD_MS, SCALE_BAD_MS),
        "io": _curve(calls_per_second, IO_GOOD_PER_S, IO_BAD_PER_S),
    }
    total = sum(parts[key] * weight / 100.0 for key, weight, _ in WEIGHTS)
    return {
        "score": round(total, 1),
        "parts": {key: round(parts[key], 1) for key in parts},
        "metrics": {
            "emitted_bytes": emitted,
            "needed_bytes": needed,
            "paint_ratio": round(paint_ratio, 4),
            "frames": frames,
            "changed_frames": changed,
            "null_frames": frames - changed,
            "latency_p50_ms": round(percentile(interactive, 0.5), 3),
            "latency_p99_ms": round(latency_p99, 3),
            "scale_p50_ms": round(percentile(scale_samples, 0.5), 3),
            "scale_p99_ms": round(scale_p99, 3),
            "idle_calls_per_s": round(calls_per_second, 2),
            "unhandled_sequences": sum(r["unhandled"] for r in results),
        },
        "scenarios": {
            r["name"]: {
                "frames": r["frames"],
                "null_frames": r["frames"] - r["changed"],
                "emitted_bytes": r["emitted"],
                "needed_bytes": r["needed"],
                "bytes_per_frame": int(r["emitted"] / r["frames"]) if r["frames"] else 0,
                "paint_ratio": round(r["needed"] / r["emitted"], 4) if r["emitted"] else 1.0,
                "calls": r["calls"],
                "p50_ms": round(percentile(r["samples"], 0.5), 3),
                "p99_ms": round(percentile(r["samples"], 0.99), 3),
            }
            for r in results
        },
    }


# -- reporting -------------------------------------------------------------


def _kb(value: int) -> str:
    if value < 1024:
        return "%d B" % value
    return "%.1f KB" % (value / 1024.0)


def _bar(value: float, width: int = 24) -> str:
    filled = int(round(width * value / 100.0))
    return "█" * filled + "·" * (width - filled)


def report(data: Dict[str, Any], scenarios: bool) -> str:
    metrics = data["metrics"]
    parts = data["parts"]
    lines = ["", "  herdr-bar render score    %5.1f / 100" % data["score"], ""]

    notes = {
        "paint": "%s emitted for %s of actual change"
        % (_kb(metrics["emitted_bytes"]), _kb(metrics["needed_bytes"])),
        "frames": "%d of %d frames changed nothing"
        % (metrics["null_frames"], metrics["frames"]),
        "latency": "p99 %.1f ms  (p50 %.1f)"
        % (metrics["latency_p99_ms"], metrics["latency_p50_ms"]),
        "scale": "p99 %.1f ms  (p50 %.1f)" % (metrics["scale_p99_ms"], metrics["scale_p50_ms"]),
        "io": "%.1f herdr calls/s while idle" % metrics["idle_calls_per_s"],
    }
    for key, weight, label in WEIGHTS:
        lines.append(
            "  %-17s %s %5.1f  ×%.2f = %5.2f   %s"
            % (label, _bar(parts[key]), parts[key], weight / 100.0,
               parts[key] * weight / 100.0, notes[key])
        )
    lines.append("")

    if scenarios:
        lines.append("  scenario     frames  null   emitted    needed  ratio   calls    p99")
        lines.append("  " + "-" * 72)
        for name, row in data["scenarios"].items():
            lines.append(
                "  %-11s %6d %5d %9s %9s %5.1f%% %6d %6.2fms"
                % (name, row["frames"], row["null_frames"], _kb(row["emitted_bytes"]),
                   _kb(row["needed_bytes"]), 100.0 * row["paint_ratio"], row["calls"],
                   row["p99_ms"])
            )
        lines.append("")

    if metrics["unhandled_sequences"]:
        lines.append("  ! %d escape sequences the screen model does not understand;"
                     " the paint numbers are approximate." % metrics["unhandled_sequences"])
        lines.append("")
    return "\n".join(lines) + "\n"


# Only the deterministic metrics gate a build. The latency numbers are
# wall-clock, and a runner that happens to be busy would fail a tree that is
# perfectly fine -- a bench that cries wolf is a bench somebody turns off.
#
# `needed_bytes` is deliberately not gated: it is the amount of real visual
# change, so it goes up when the bar grows a feature, which is not a
# regression. What must not slip is the share of the payload doing work.
TOLERANCE = 0.02
GATED = (
    ("emitted_bytes", "up", "bytes pushed to the terminal"),
    ("null_frames", "up", "frames that repainted an identical screen"),
    ("paint_ratio", "down", "share of the payload that was actual change"),
)


def check(data: Dict[str, Any], baseline: Dict[str, Any]) -> Tuple[bool, List[str]]:
    problems: List[str] = []
    for key, bad_direction, label in GATED:
        now = data["metrics"][key]
        was = baseline.get("metrics", {}).get(key)
        if was is None:
            continue
        if bad_direction == "up":
            regressed = now > was and now > was * (1.0 + TOLERANCE)
        else:
            regressed = now < was and now < was * (1.0 - TOLERANCE)
        if regressed:
            problems.append(
                "  %s: %s -> %s   (%s)" % (key, _short(was), _short(now), label)
            )
    return (not problems), problems


def _short(value: Any) -> str:
    if isinstance(value, float):
        return "%.3f" % value
    return str(value)


def main(argv: Sequence[str]) -> int:
    results = run_all()
    data = score(results)

    if "--json" in argv:
        sys.stdout.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        return 0

    if "--save" in argv:
        with open(BASELINE_PATH, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        sys.stdout.write(report(data, "--scenarios" in argv))
        sys.stdout.write("  saved %s\n\n" % os.path.relpath(BASELINE_PATH, ROOT))
        return 0

    sys.stdout.write(report(data, "--scenarios" in argv))

    if "--check" in argv:
        if not os.path.exists(BASELINE_PATH):
            sys.stdout.write("  no baseline yet -- run with --save\n\n")
            return 0
        with open(BASELINE_PATH, "r", encoding="utf-8") as handle:
            baseline = json.load(handle)
        ok, problems = check(data, baseline)
        if ok:
            sys.stdout.write("  no regression against the baseline.\n\n")
            return 0
        sys.stdout.write("  regressed against the baseline:\n")
        sys.stdout.write("\n".join(problems) + "\n\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
