#!/usr/bin/env python3
"""Film the bar driving a fake, very busy Herdr session.

    python3 scripts/record_demo.py                 # demo/herdr-bar.mp4 + .gif
    python3 scripts/record_demo.py --no-gif
    python3 scripts/record_demo.py --stills        # one PNG per scene, no video
    python3 scripts/record_demo.py --frame 120     # a single PNG, for tweaking

The real ``Bar`` runs against ``scripts/busy_session.py``. Nothing is faked at
the drawing layer: the recorder feeds keys into the same handlers the terminal
would, on a virtual clock so spinners, the preview debounce and the preview
cache all behave exactly as they do live, then rasterises the escape sequences
the bar emits and hands the frames to ffmpeg.

Needs Pillow and ffmpeg. Everything else is the plugin itself.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import types
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import busy_session  # noqa: E402

from herdr_bar import app as app_module  # noqa: E402
from herdr_bar.app import SPINNER_INTERVAL, Bar  # noqa: E402
from herdr_bar.config import Config  # noqa: E402
from herdr_bar.keys import KEY, TEXT, Event  # noqa: E402
from herdr_bar.mru import Recents  # noqa: E402
from herdr_bar.textutil import char_width  # noqa: E402
from herdr_bar.theme import Theme  # noqa: E402

COLS, ROWS = 104, 22
FPS = 20

FONT_DIRS = (
    os.path.expanduser("~/Library/Fonts"),
    "/Library/Fonts",
    "/System/Library/Fonts",
    "/usr/share/fonts/truetype",
)
FONT_CANDIDATES = (
    ("IosevkaTerm-Extended.ttf", "IosevkaTerm-ExtendedBold.ttf"),
    ("JetBrainsMono-Regular.ttf", "JetBrainsMono-Bold.ttf"),
    ("FiraCode-Regular.ttf", "FiraCode-Bold.ttf"),
    ("DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf"),
    ("Menlo.ttc", "Menlo.ttc"),
)

FONT_SIZE = 30
CAPTION_SIZE = 27
CHIP_SIZE = 24
LINE_RATIO = 1.36

CANVAS_BG = (10, 11, 15)
PANEL_BG = (20, 22, 29)
PANEL_EDGE = (44, 49, 62)
PANEL_GLOW = (30, 34, 44)
CAPTION_FG = (206, 213, 227)
CAPTION_DIM = (110, 118, 136)
CHIP_BG = (32, 36, 47)
CHIP_EDGE = (64, 71, 88)
CHIP_FG = (214, 221, 235)
DEFAULT_FG = (211, 216, 227)

# The 16 ANSI slots, in a palette that suits the dark canvas. Everything the
# theme asks for is either one of these, a 256-cube index, or a hex triple.
BASE16 = [
    (26, 28, 35),
    (226, 112, 122),
    (127, 184, 131),
    (215, 176, 106),
    (111, 158, 217),
    (185, 138, 209),
    (99, 179, 196),
    (200, 205, 216),
    (107, 114, 128),
    (255, 123, 134),
    (143, 220, 154),
    (255, 212, 121),
    (130, 183, 255),
    (215, 163, 240),
    (126, 224, 232),
    (238, 241, 247),
]

ROW_RE = re.compile(r"\x1b\[(\d+);1H(.*?)(?=\x1b\[\d+;1H|\x1b\[J|$)", re.S)
SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")

KEYCAPS = {
    "up": "↑",
    "down": "↓",
    "left": "←",
    "right": "→",
    "tab": "⇥",
    "enter": "⏎",
    "backspace": "⌫",
    "delete": "⌦",
    "y": "y",
    "esc": "esc",
    "ctrl+u": "^U",
    "ctrl+o": "^O",
    "ctrl+w": "^W",
    "pgdn": "⇟",
    "pgup": "⇞",
}
CHIP_TTL = 0.7


# -- palette -----------------------------------------------------------------


def xterm256() -> List[Tuple[int, int, int]]:
    palette = list(BASE16)
    levels = (0, 95, 135, 175, 215, 255)
    for red in levels:
        for green in levels:
            for blue in levels:
                palette.append((red, green, blue))
    for step in range(24):
        value = 8 + step * 10
        palette.append((value, value, value))
    return palette


PALETTE = xterm256()


# -- the session under the camera --------------------------------------------


class Clock(object):
    """A monotonic clock the recorder owns, so every frame is reproducible."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class DemoClient(object):
    socket_path = None
    bin_path = "herdr"
    socket_ok = True

    def __init__(self) -> None:
        self.state = busy_session.snapshot()
        self.overrides: Dict[str, str] = {}

    def snapshot(self):
        return self.state

    def read_pane(self, pane_id, lines, source="visible"):
        if pane_id in self.overrides:
            return self.overrides[pane_id]
        return busy_session.PREVIEWS.get(pane_id, busy_session.FALLBACK_PREVIEW)

    def pane_age(self, pane_id):
        return busy_session.AGES.get(pane_id)

    def close_tab(self, tab_id):
        """Take the tab out of the session, the way herdr would."""
        self.state["tabs"] = [tab for tab in self.state["tabs"] if tab.get("tab_id") != tab_id]
        self.state["panes"] = [pane for pane in self.state["panes"] if pane.get("tab_id") != tab_id]
        self.state["agents"] = [
            agent for agent in self.state["agents"] if agent.get("tab_id") != tab_id
        ]

    def set_status(self, pane_id: str, status: str) -> None:
        """Move one agent to another state, the way a live session would."""
        for group in ("agents", "panes", "tabs"):
            for record in self.state[group]:
                matches = record.get("pane_id") == pane_id or (
                    group == "tabs" and record.get("tab_id") == pane_id.replace(":p", ":t")
                )
                if matches:
                    record["agent_status"] = status
        after = busy_session.PREVIEWS_AFTER.get((pane_id, status))
        if after:
            self.overrides[pane_id] = after

    def focus_workspace(self, workspace_id):
        pass

    def focus_tab(self, tab_id):
        pass

    def focus_agent(self, target):
        pass


class Screen(object):
    def __init__(self, width: int, height: int) -> None:
        self._size = (width, height)
        self.buffer: List[str] = []

    def size(self):
        return self._size

    def write(self, text: str) -> None:
        self.buffer.append(text)

    def flush(self) -> None:
        pass


class Frame(object):
    __slots__ = ("rows", "caption", "chip", "chip_age", "outcome")

    def __init__(
        self, rows: List[str], caption: str, chip: Optional[str], chip_age: float, outcome: str
    ) -> None:
        self.rows = rows
        self.caption = caption
        self.chip = chip
        self.chip_age = chip_age
        self.outcome = outcome


class Recorder(object):
    """Runs the bar forward in virtual time, collecting one frame per tick."""

    def __init__(self, fps: int = FPS) -> None:
        self.clock = Clock()
        app_module.time = types.SimpleNamespace(monotonic=self.clock.monotonic)

        self.client = DemoClient()
        recents = Recents(None)
        for key in reversed(busy_session.RECENTS):
            recents.touch(key)

        self.bar = Bar(
            self.client,
            Config({"selection_background": "237"}),
            recents,
            Theme(selection_background="237"),
        )
        self.bar.bootstrap()

        self.screen = Screen(COLS, ROWS)
        self.fps = fps
        self.dt = 1.0 / fps
        self.frames: List[Frame] = []
        self.scenes: List[Tuple[int, str]] = []
        self.caption = ""
        self.chip: Optional[str] = None
        self.chip_at = -99.0
        self.outcome = ""
        self._last_spin = self.clock.now

    # -- timeline ----------------------------------------------------------

    def say(self, caption: str) -> None:
        self.caption = caption
        self.scenes.append((len(self.frames), caption))

    def hold(self, seconds: float) -> None:
        for _ in range(max(1, int(round(seconds * self.fps)))):
            self._tick()

    def key(self, name: str, dwell: float = 0.34) -> None:
        if self.bar.pending_close is not None:
            # An armed close owns the keyboard, exactly as the run loop has it.
            self.bar.handle_confirm(Event(KEY, name))
        else:
            result = self.bar.handle_key(name)
            if result == "confirm" and self.bar.rows:
                item = self.bar.selected_item()
                self.outcome = "jumped to %s" % item.title
        self.chip = KEYCAPS.get(name, name)
        self.chip_at = self.clock.now
        self.hold(dwell)

    def repeat(self, name: str, times: int, dwell: float = 0.3) -> None:
        for _ in range(times):
            self.key(name, dwell)

    def type(self, text: str, dwell: float = 0.13) -> None:
        for char in text:
            if self.bar.pending_close is not None:
                self.bar.handle_confirm(Event(TEXT, char))
            else:
                self.bar.insert(char)
            self.chip = char
            self.chip_at = self.clock.now
            self.hold(dwell)

    def restatus(self, pane_id: str, status: str) -> None:
        self.client.set_status(pane_id, status)
        self.bar._preview_cache.pop(pane_id, None)
        self.bar.refresh()

    # -- one frame ---------------------------------------------------------

    def _tick(self) -> None:
        self.clock.now += self.dt

        if self.clock.now - self._last_spin >= SPINNER_INTERVAL:
            self._last_spin = self.clock.now
            if any(row.item.status == "working" for row in self.bar.rows):
                self.bar.tick += 1

        self.bar.pump_preview(self.bar._list_height)
        self.bar.pump_ages()

        self.screen.buffer = []
        self.bar.draw(self.screen)
        rows = flatten("".join(self.screen.buffer), ROWS)

        age = self.clock.now - self.chip_at
        self.frames.append(
            Frame(rows, self.caption, self.chip if age < CHIP_TTL else None, age, self.outcome)
        )


def flatten(frame: str, height: int) -> List[str]:
    """Absolute-positioned output back into plain rows, colors intact."""
    rows = [""] * height
    for match in ROW_RE.finditer(frame):
        index = int(match.group(1)) - 1
        if 0 <= index < height:
            rows[index] = match.group(2).replace("\x1b[K", "")
    return rows


# -- the script --------------------------------------------------------------


def perform(recorder: Recorder) -> None:
    bar = recorder.bar

    recorder.say("one chord, and the whole session is a list")
    recorder.hold(1.3)

    recorder.say("the tab you named, what its agent is doing, how long it has run")
    recorder.hold(1.9)

    recorder.say("needs-you first, then done · where you just were floats up")
    recorder.hold(1.5)

    recorder.say("↑↓ moves · the right column tails the pane you land on")
    recorder.repeat("down", 4, 0.5)
    recorder.hold(0.7)

    recorder.say("twenty-one rows, and it scrolls")
    recorder.repeat("down", 7, 0.19)
    recorder.hold(1.1)

    recorder.say("type a few letters · fuzzy, and it underlines what matched")
    recorder.key("ctrl+u", 0.2)
    recorder.type("invo", 0.16)
    recorder.hold(2.2)

    recorder.say("wherever it landed · the tab name here, the summary there")
    recorder.key("ctrl+u", 0.22)
    recorder.type("cache api", 0.15)
    recorder.hold(2.2)

    recorder.say("terms match independently · title, repo, branch, agent")
    recorder.key("ctrl+u", 0.22)
    recorder.type("ret q", 0.15)
    recorder.hold(2.0)

    recorder.say("nothing matching says so, and tells you the way out")
    recorder.key("ctrl+u", 0.22)
    recorder.type("zzz", 0.15)
    recorder.hold(1.6)

    recorder.say("! narrows to the agents waiting on you")
    recorder.key("ctrl+u", 0.25)
    recorder.type("!", 0.2)
    recorder.hold(2.1)

    recorder.say("⇥ cycles the scopes · everything, agents, shells, needs you")
    recorder.key("ctrl+u", 0.25)
    recorder.repeat("tab", 3, 1.15)
    recorder.key("tab", 0.8)

    recorder.say("^o folds the preview away when you want the room")
    recorder.key("ctrl+o", 1.7)
    recorder.key("ctrl+o", 0.9)

    recorder.say("and it keeps updating while it is open")
    recorder.hold(0.8)
    recorder.restatus("w1:p7", "blocked")
    recorder.hold(2.3)

    recorder.say("⌦ closes a tab · the footer asks first, esc calls it off")
    recorder.key("ctrl+u", 0.25)
    recorder.type("logs", 0.16)
    recorder.hold(0.9)
    recorder.key("delete", 1.7)
    recorder.key("esc", 1.1)

    recorder.say("⏎ jumps · tab focused, pane focused, popup gone")
    recorder.key("ctrl+u", 0.22)
    recorder.type("limit", 0.16)
    recorder.hold(1.3)
    recorder.key("enter", 0.1)
    recorder.hold(2.0)

    if not bar.rows:  # pragma: no cover - guards the script, not the bar
        raise SystemExit("the demo script ended on an empty list")


# -- rasterising -------------------------------------------------------------


class Cell(object):
    __slots__ = ("char", "fg", "bg", "bold", "underline")

    def __init__(self, char: str, fg, bg, bold: bool, underline: bool = False) -> None:
        self.char = char
        self.fg = fg
        self.bg = bg
        self.bold = bold
        self.underline = underline


def parse_row(row: str, cols: int) -> List[Cell]:
    """Turn one escape-laden row into cells, honouring 38/48/1/4/7 and reset."""
    fg: Optional[Tuple[int, int, int]] = None
    bg: Optional[Tuple[int, int, int]] = None
    bold = False
    underline = False
    reverse = False

    cells: List[Cell] = []
    index = 0
    while index < len(row) and len(cells) < cols:
        if row.startswith("\x1b[", index):
            match = SGR_RE.match(row, index)
            if match:
                fg, bg, bold, underline, reverse = apply_sgr(
                    match.group(1), fg, bg, bold, underline, reverse
                )
                index = match.end()
                continue
            end = index + 2
            while end < len(row) and not ("@" <= row[end] <= "~"):
                end += 1
            index = end + 1
            continue
        char = row[index]
        index += 1
        if char_width(char) == 0:
            continue
        front = fg if fg is not None else DEFAULT_FG
        back = bg
        if reverse:
            front, back = (back if back is not None else PANEL_BG), front
        cells.append(Cell(char, front, back, bold, underline))
        if char_width(char) == 2:
            cells.append(Cell("", front, back, bold, underline))
    while len(cells) < cols:
        cells.append(Cell(" ", DEFAULT_FG, None, False))
    return cells[:cols]


def apply_sgr(params: str, fg, bg, bold: bool, underline: bool, reverse: bool):
    codes = [int(part) if part else 0 for part in (params or "0").split(";")]
    index = 0
    while index < len(codes):
        code = codes[index]
        if code == 0:
            fg, bg, bold, underline, reverse = None, None, False, False, False
        elif code == 1:
            bold = True
        elif code == 22:
            bold = False
        elif code == 4:
            underline = True
        elif code == 24:
            underline = False
        elif code == 7:
            reverse = True
        elif code == 27:
            reverse = False
        elif code in (38, 48):
            target = None
            if index + 2 < len(codes) and codes[index + 1] == 5:
                target = PALETTE[codes[index + 2] % 256]
                index += 2
            elif index + 4 < len(codes) and codes[index + 1] == 2:
                target = tuple(codes[index + 2 : index + 5])
                index += 4
            if code == 38:
                fg = target
            else:
                bg = target
        elif code == 39:
            fg = None
        elif code == 49:
            bg = None
        elif 30 <= code <= 37:
            fg = PALETTE[code - 30]
        elif 90 <= code <= 97:
            fg = PALETTE[code - 90 + 8]
        elif 40 <= code <= 47:
            bg = PALETTE[code - 40]
        elif 100 <= code <= 107:
            bg = PALETTE[code - 100 + 8]
        index += 1
    return fg, bg, bold, underline, reverse


def find_font(bold: bool):
    from PIL import ImageFont

    for regular, heavy in FONT_CANDIDATES:
        name = heavy if bold else regular
        for directory in FONT_DIRS:
            path = os.path.join(directory, name)
            if os.path.exists(path):
                return ImageFont.truetype(path, FONT_SIZE)
    raise SystemExit("no monospace font found; install Iosevka Term or JetBrains Mono")


def ui_font(size: int, bold: bool = False):
    from PIL import ImageFont

    for candidate in (
        "/System/Library/Fonts/SFNSDisplay.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ):
        if os.path.exists(candidate):
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    for regular, heavy in FONT_CANDIDATES:
        name = heavy if bold else regular
        for directory in FONT_DIRS:
            path = os.path.join(directory, name)
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
    raise SystemExit("no UI font found")


_COVERAGE: Dict[Tuple[int, str], bool] = {}


def covers(font, char: str) -> bool:
    """Whether a font has a real glyph for a character.

    Pillow draws .notdef -- an empty box -- for anything missing and says
    nothing about it, which is how ⇥ and ⌦ ended up as tofu in the captions.
    Comparing against a private-use codepoint no font claims spots it.
    """
    if char.isspace():
        return True
    key = (id(font), char)
    if key not in _COVERAGE:
        try:
            missing = bytes(font.getmask("", mode="1"))
            _COVERAGE[key] = bytes(font.getmask(char, mode="1")) != missing
        except Exception:  # pragma: no cover - font backends vary
            _COVERAGE[key] = True
    return _COVERAGE[key]


def runs_by_font(text: str, font, fallback):
    """Split text into runs, each with a font that can actually draw it."""
    out: List[Tuple[str, object]] = []
    for char in text:
        chosen = font if covers(font, char) else fallback
        if out and out[-1][1] is chosen:
            out[-1] = (out[-1][0] + char, chosen)
        else:
            out.append((char, chosen))
    return out


def draw_text(draw, xy, text: str, font, fallback, fill) -> float:
    """Draw a line in `font`, borrowing `fallback` for what it cannot render."""
    x, y = xy
    for run, run_font in runs_by_font(text, font, fallback):
        draw.text((x, y), run, font=run_font, fill=fill)
        x += draw.textlength(run, font=run_font)
    return x - xy[0]


def text_length(draw, text: str, font, fallback) -> float:
    return sum(
        draw.textlength(run, font=run_font) for run, run_font in runs_by_font(text, font, fallback)
    )


class Painter(object):
    def __init__(self, compact: bool = False) -> None:
        self.font = find_font(False)
        self.font_bold = find_font(True)
        self.caption_font = ui_font(CAPTION_SIZE)
        self.chip_font = ui_font(CHIP_SIZE, bold=True)
        # The keycaps in the captions are terminal glyphs; the UI font has no
        # idea what ⇥ or ⌦ are, so the monospace face stands in for them.
        self.caption_fallback = find_font(False).font_variant(size=CAPTION_SIZE)
        self.chip_fallback = find_font(False).font_variant(size=CHIP_SIZE)

        self.cell_w = int(math.ceil(self.font.getlength("M")))
        self.cell_h = int(math.ceil(FONT_SIZE * LINE_RATIO))
        # Compact trims the dead canvas around the panel, so a README-width
        # GIF spends its pixels on the text instead of the margin.
        self.pad = 22 if compact else 30
        self.margin = 26 if compact else 52
        strip = 82 if compact else 120

        panel_w = COLS * self.cell_w + self.pad * 2
        panel_h = ROWS * self.cell_h + self.pad * 2
        self.panel = (self.margin, self.margin, self.margin + panel_w, self.margin + panel_h)
        width = panel_w + self.margin * 2
        height = panel_h + self.margin + strip
        self.size = (width + width % 2, height + height % 2)

        # The baseline nudge keeps glyphs optically centred in their cell.
        ascent, _ = self.font.getmetrics()
        self.baseline = (self.cell_h - ascent) // 2

    def render(self, frame: Frame):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", self.size, CANVAS_BG)
        draw = ImageDraw.Draw(image)

        left, top, right, bottom = self.panel
        draw.rounded_rectangle(
            (left - 6, top - 6, right + 6, bottom + 6), radius=20, fill=PANEL_GLOW
        )
        draw.rounded_rectangle(
            (left, top, right, bottom), radius=15, fill=PANEL_BG, outline=PANEL_EDGE, width=2
        )

        origin_x = left + self.pad
        origin_y = top + self.pad
        for index, row in enumerate(frame.rows):
            cells = parse_row(row, COLS)
            y = origin_y + index * self.cell_h
            run_start = 0
            while run_start < COLS:
                back = cells[run_start].bg
                run_end = run_start
                while run_end < COLS and cells[run_end].bg == back:
                    run_end += 1
                if back is not None:
                    draw.rectangle(
                        (
                            origin_x + run_start * self.cell_w,
                            y,
                            origin_x + run_end * self.cell_w - 1,
                            y + self.cell_h - 1,
                        ),
                        fill=back,
                    )
                run_start = run_end
            for column, cell in enumerate(cells):
                if cell.underline:
                    # Drawn per cell so a run stays continuous across spaces.
                    rule_y = y + self.cell_h - max(2, self.cell_h // 12)
                    draw.rectangle(
                        (
                            origin_x + column * self.cell_w,
                            rule_y,
                            origin_x + (column + 1) * self.cell_w - 1,
                            rule_y + max(1, self.cell_h // 18),
                        ),
                        fill=cell.fg,
                    )
                if cell.char in ("", " "):
                    continue
                draw.text(
                    (origin_x + column * self.cell_w, y + self.baseline),
                    cell.char,
                    font=self.font_bold if cell.bold else self.font,
                    fill=cell.fg,
                )

        caption_y = bottom + 34
        if frame.outcome:
            draw_text(
                draw,
                (left + 4, caption_y),
                frame.outcome,
                self.caption_font,
                self.caption_fallback,
                BASE16[12],
            )
        elif frame.caption:
            draw_text(
                draw,
                (left + 4, caption_y),
                frame.caption,
                self.caption_font,
                self.caption_fallback,
                CAPTION_FG,
            )

        if frame.chip:
            self._chip(draw, frame, right, caption_y)
        return image

    def _chip(self, draw, frame: Frame, right: int, y: int) -> None:
        label = frame.chip
        text_w = int(text_length(draw, label, self.chip_font, self.chip_fallback))
        width = max(52, text_w + 30)
        height = 46
        box = (right - width + 4, y - 8, right + 4, y - 8 + height)
        fade = max(0.0, 1.0 - (frame.chip_age / CHIP_TTL) ** 2)
        fill = blend(CANVAS_BG, CHIP_BG, fade)
        edge = blend(CANVAS_BG, CHIP_EDGE, fade)
        draw.rounded_rectangle(box, radius=11, fill=fill, outline=edge, width=2)
        draw_text(
            draw,
            (box[0] + (width - text_w) / 2, y + 1),
            label,
            self.chip_font,
            self.chip_fallback,
            blend(CANVAS_BG, CHIP_FG, fade),
        )


def blend(back, front, amount: float):
    return tuple(int(back[i] + (front[i] - back[i]) * amount) for i in range(3))


# -- driving -----------------------------------------------------------------


def encode(
    frames_dir: str, out_path: str, fps: int, gif: bool, gif_width: int, gif_fps: int
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg not found; frames are in %s" % frames_dir)
    pattern = os.path.join(frames_dir, "f%05d.png")
    subprocess.run(
        [
            ffmpeg, "-y", "-loglevel", "error",
            "-framerate", str(fps), "-i", pattern,
            "-c:v", "libx264", "-preset", "slow", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            out_path,
        ],
        check=True,
    )
    if not gif:
        return
    # Built from the PNGs, not from the mp4: h264 chroma artefacts turn into
    # visible palette noise once the GIF quantises them.
    palette = os.path.join(frames_dir, "palette.png")
    chain = "fps=%d,scale=%d:-1:flags=lanczos" % (gif_fps, gif_width)
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-framerate", str(fps), "-i", pattern,
         "-vf", chain + ",palettegen=stats_mode=diff:max_colors=192", palette],
        check=True,
    )
    use = "paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle"
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-framerate", str(fps), "-i", pattern,
         "-i", palette,
         "-lavfi", chain + " [x]; [x][1:v] " + use,
         "-loop", "0",
         os.path.splitext(out_path)[0] + ".gif"],
        check=True,
    )


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=os.path.join(ROOT, "demo"))
    parser.add_argument("--fps", type=int, default=FPS)
    parser.add_argument("--name", default="herdr-bar")
    parser.add_argument("--no-gif", dest="gif", action="store_false")
    parser.add_argument("--gif-width", type=int, default=1080)
    parser.add_argument("--gif-fps", type=int, default=12)
    parser.add_argument("--compact", action="store_true", help="tighter framing, for a README")
    parser.add_argument("--stills", action="store_true", help="one PNG per scene, no video")
    parser.add_argument("--frame", type=int, help="render a single frame index and stop")
    parser.add_argument("--keep-frames", action="store_true")
    options = parser.parse_args(list(argv))

    try:
        import PIL  # noqa: F401
    except ImportError:
        raise SystemExit("Pillow is required: pip install pillow")

    recorder = Recorder(options.fps)
    perform(recorder)
    painter = Painter(compact=options.compact)
    os.makedirs(options.out, exist_ok=True)

    total = len(recorder.frames)
    seconds = total / float(options.fps)
    sys.stderr.write("%d frames · %.1fs · %dx%d\n" % (total, seconds, *painter.size))

    if options.frame is not None:
        index = max(0, min(options.frame, total - 1))
        path = os.path.join(options.out, "frame-%05d.png" % index)
        painter.render(recorder.frames[index]).save(path)
        sys.stderr.write("%s\n" % path)
        return 0

    if options.stills:
        for order, (start, caption) in enumerate(recorder.scenes):
            index = min(total - 1, start + int(options.fps * 1.1))
            slug = re.sub(r"[^a-z0-9]+", "-", caption.lower()).strip("-")[:44]
            path = os.path.join(options.out, "scene-%02d-%s.png" % (order + 1, slug))
            painter.render(recorder.frames[index]).save(path)
            sys.stderr.write("%s\n" % path)
        return 0

    frames_dir = os.path.join(options.out, ".frames")
    if os.path.isdir(frames_dir):
        shutil.rmtree(frames_dir)
    os.makedirs(frames_dir)
    for index, frame in enumerate(recorder.frames):
        painter.render(frame).save(os.path.join(frames_dir, "f%05d.png" % index))
        if index % 50 == 0:
            sys.stderr.write("\r  rendering %d/%d" % (index, total))
            sys.stderr.flush()
    sys.stderr.write("\r  rendering %d/%d\n" % (total, total))

    out_path = os.path.join(options.out, options.name + ".mp4")
    encode(
        frames_dir, out_path, options.fps, options.gif, options.gif_width, options.gif_fps
    )
    if not options.keep_frames:
        shutil.rmtree(frames_dir)
    sys.stderr.write("%s\n" % out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
