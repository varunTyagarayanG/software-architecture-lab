#!/usr/bin/env python3
"""
Login API rate limiting: a ~50 second live terminal demo.

Four acts, each one a real limiter being attacked while you watch:
  1. Fixed window    the counter does its job inside one minute
  2. Boundary trap   ...and lets double the limit through across the reset
  3. Token bucket    the same attack, without the hole
  4. 10 servers      a counter per server vs. one shared counter in Redis

After each act the show stops on a summary of what just happened and
waits for Enter before it moves on.

It asks how you want to watch: step by step, normal, slow or fast.

Run:  python simulate.py              full show, Enter between acts
      python simulate.py --auto       no stops, plays straight through (~50 s)
      python simulate.py 2 3          only these acts
      python simulate.py --speed 0.5  half speed (2 = twice as fast)

Needs the 'rich' library (pip install rich) and a terminal of at least 80x24.
"""

from __future__ import annotations

import argparse
import math
import os
import select
import sys
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

try:
    import termios
except ImportError:  # Windows: keys are echoed and Enter is read as a whole line
    termios = None

try:
    from rich import box
    from rich.align import Align
    from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
    from rich.layout import Layout
    from rich.live import Live
    from rich.padding import Padding
    from rich.panel import Panel
    from rich.progress_bar import ProgressBar
    from rich.table import Table
    from rich.text import Text
except ImportError:
    sys.exit("This demo draws with the 'rich' library. Install it with:  pip install rich")

# Demo uses 10/min so each request is visible; logic matches 100/min in notes.
LIMIT = 10
WINDOW = 60
SERVERS = 10
EPS = 1e-9

TITLE = "LOGIN API · RATE LIMITING"

MIN_WIDTH, MIN_HEIGHT = 80, 24
FPS = 24
PACE = 1.25  # stretches every pause in the show; raise it to slow everything down

OK = "bright_green"
BAD = "bright_red"
ACCENT = "cyan"

ACT_NAMES = ["Fixed window", "Boundary trap", "Token bucket", "10 servers"]
INTRO, FINALE = 0, len(ACT_NAMES) + 1


# --- Fixed window counter ---------------------------------------------------

@dataclass
class FixedWindow:
    """One counter per user, wiped whenever the clock enters a new window."""

    limit: int = LIMIT
    window_sec: int = WINDOW
    count: int = 0
    window: int = 0

    def roll(self, now: float) -> None:
        window = int(now // self.window_sec)
        if window != self.window:
            self.window, self.count = window, 0

    def allow(self, now: float) -> bool:
        self.roll(now)
        if self.count >= self.limit:
            return False
        self.count += 1
        return True


# --- Token bucket -----------------------------------------------------------

@dataclass
class TokenBucket:
    """Each request spends a token; tokens drip back at a steady rate."""

    capacity: int = LIMIT
    refill_per_sec: float = LIMIT / WINDOW
    tokens: float = float(LIMIT)
    updated: float = 0.0

    def refill(self, now: float) -> None:
        gained = (now - self.updated) * self.refill_per_sec
        self.tokens = min(self.capacity, self.tokens + gained)
        self.updated = now

    def allow(self, now: float) -> bool:
        self.refill(now)
        # EPS: six refills of 1/6 token must add up to one whole token.
        if self.tokens < 1 - EPS:
            return False
        self.tokens = max(0.0, self.tokens - 1)
        return True


# --- Drawing helpers --------------------------------------------------------

def mmss(seconds: float) -> str:
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def section(title: str, hint: str = "") -> Text:
    text = Text(title, style="bold")
    if hint:
        text.append(f"  {hint}", style="dim")
    return text


def slots(filled: int, total: int, on: str = "■", off: str = "□", style: str = ACCENT, sep: str = " ") -> Text:
    text = Text()
    for i in range(total):
        if i:
            text.append(sep)
        text.append(on if i < filled else off, style=style if i < filled else "dim")
    return text


def through_bar(count: int, width: int = 0) -> Text:
    """One block per login let through: green up to the limit, red beyond it."""
    over = max(0, count - LIMIT)
    text = Text()
    text.append("█" * (count - over), style=OK)
    text.append("█" * over, style=BAD)
    text.append(f" {count}", style=f"bold {BAD if over else OK}")
    if over:
        for note in (f" · {over} over the limit", f" · +{over}"):
            if text.cell_len + len(note) <= width:
                text.append(note, style=BAD)
                break
    return text


def stack(blocks: list[list[RenderableType]], height: int) -> RenderableType:
    """Stack blocks of lines, spending spare rows on blank lines between them."""
    spare = height - sum(len(block) for block in blocks)
    rows: list[RenderableType] = []
    for i, block in enumerate(blocks):
        if 0 < i <= spare:
            rows.append(Text())
        rows.extend(block)
    return Align.center(Group(*rows), vertical="middle", height=height)


class Sized:
    """A renderable that is told how much room it has before it draws."""

    def __init__(self, build: Callable[[int, int], RenderableType]) -> None:
        self.build = build

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield self.build(options.max_width, options.height or options.max_height)


@dataclass
class Hit:
    clock: float
    label: str
    ok: bool


SPARKS = "▂▃▄▅▆▇█"


def timeline(hits: list[Hit], now: float, width: int) -> list[Text]:
    """Two minutes side by side; bar height = logins that landed in that slice."""
    n = max(12, min(30, (width - 1) // 2))  # cells per minute
    allowed, blocked = [0] * (2 * n), [0] * (2 * n)
    for hit in hits:
        cell = min(2 * n - 1, int(hit.clock / WINDOW * n))
        (allowed if hit.ok else blocked)[cell] += 1

    def spark(count: int) -> str:
        return SPARKS[min(len(SPARKS), math.ceil(count / LIMIT * len(SPARKS))) - 1]

    strip = Text()
    for cell in range(2 * n):
        if cell == n:
            strip.append("│", style="bold yellow")
        if allowed[cell]:
            strip.append(spark(allowed[cell]), style=OK)
        elif blocked[cell]:
            strip.append(spark(blocked[cell]), style=BAD)
        else:
            strip.append("▁", style="dim")

    labels = Text(f"╰{' minute 1 ':─^{n - 2}}╯ ╰{' minute 2 ':─^{n - 2}}╯", style="dim")

    # The boundary itself sits in the column between the two minutes.
    pos = min(2 * n, int(now / WINDOW * n) + (1 if now > WINDOW else 0))
    stamp = mmss(now)
    if pos + len(stamp) + 2 <= 2 * n + 1:
        cursor = Text(" " * pos + f"▲ {stamp}", style=f"bold {ACCENT}")
    else:
        cursor = Text(" " * (pos - len(stamp) - 1) + f"{stamp} ▲", style=f"bold {ACCENT}")
    return [strip, labels, cursor]


def timeline_block(show: Show, width: int) -> list[RenderableType]:
    head = Text("TIMELINE  ", style="bold")
    head.append("█", style=OK)
    head.append(" allowed  ", style="dim")
    head.append("█", style=BAD)
    head.append(" blocked", style="dim")
    return [head, *timeline(show.hits, show.clock, width)]


class LogView:
    """Scrolling request log that shows as much detail as its width allows."""

    def __init__(self, entries: deque[Hit | str]) -> None:
        self.entries = entries

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = options.max_width
        visible = list(self.entries)[-(options.height or 10):]
        for i, entry in enumerate(visible):
            if isinstance(entry, str):
                yield Text(f"{' ' + entry + ' ':─^{width}}", style="dim")
                continue
            newest = i == len(visible) - 1
            line = Text(f"{entry.label:<9}  ", style="bold" if newest else "")
            if width >= 51:
                line.append("POST /api/login  ", style="dim")
            if entry.ok:
                status = "✓ 200 OK"
            else:
                status = "✗ 429 Too Many Requests" if width >= 34 else "✗ 429 rejected"
            color = OK if entry.ok else BAD
            line.append(status, style=f"bold {color}" if newest else color)
            yield line


# --- Keyboard ---------------------------------------------------------------

@contextmanager
def silent_keyboard() -> Iterator[None]:
    """Stop typed keys from being echoed over the picture (Ctrl+C still works)."""
    if termios is None or not sys.stdin.isatty():
        yield
        return
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    quiet = termios.tcgetattr(fd)
    quiet[3] &= ~(termios.ICANON | termios.ECHO)
    termios.tcsetattr(fd, termios.TCSADRAIN, quiet)
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def wait_for_enter(redraw: Callable[[], None], *, flush: bool = True) -> None:
    """Block until Enter, redrawing meanwhile so the prompt blinks and resizes show.

    `flush` drops anything typed earlier, so a key pressed while an act was
    playing cannot skip past its summary. Stepping turns that off, so the
    viewer can hold Enter down and move along.
    """
    if termios is None:
        redraw()
        sys.stdin.readline()
        return
    fd = sys.stdin.fileno()
    if flush:
        termios.tcflush(fd, termios.TCIFLUSH)
    while True:
        redraw()
        if select.select([fd], [], [], 0.1)[0] and os.read(fd, 1) in (b"\n", b"\r", b""):
            return


# --- How to watch -----------------------------------------------------------

@dataclass(frozen=True)
class Mode:
    """One way of playing the show, offered on the menu at startup."""

    key: str
    name: str
    hint: str
    speed: float
    stepping: bool


MODES = [
    Mode("1", "Step by step", "every step waits for Enter", 1.0, True),
    Mode("2", "Normal", "plays itself", 1.0, False),
    Mode("3", "Slow", "half speed", 0.5, False),
    Mode("4", "Fast", "double speed", 2.0, False),
]
DEFAULT_MODE = MODES[1]


def menu(rehearsal: "Show") -> RenderableType:
    """The opening screen, with how long each mode will take."""
    rows = Table.grid(padding=(0, 2))
    rows.add_column(style=f"bold {ACCENT}", no_wrap=True)
    rows.add_column(style="bold", no_wrap=True)
    rows.add_column(style="dim")
    rows.add_column(justify="right", style="dim", no_wrap=True)
    for mode in MODES:
        length = f"{rehearsal.step_no} steps" if mode.stepping else f"about {mmss(rehearsal.elapsed / mode.speed)}"
        rows.add_row(mode.key, mode.name, mode.hint, length)
    return Panel(
        Group(
            Text("How do you want to watch?", style="bold"),
            Text(),
            rows,
            Text(),
            Text("Every mode stops on a summary after each act.", style="dim"),
            Text.from_markup(f"[dim]Press 1-4, or Enter for {DEFAULT_MODE.name}.  Ctrl+C to quit.[/]"),
        ),
        title=f"[bold {ACCENT}]{TITLE}[/]",
        title_align="left",
        border_style=ACCENT,
        padding=(1, 2),
        expand=False,
    )


def choose_mode(console: Console, rehearsal: "Show") -> Mode:
    """Show the menu and wait for a choice."""
    console.print(menu(rehearsal))
    keys = {mode.key: mode for mode in MODES}
    if termios is None:  # Windows: read a whole line instead of one key
        try:
            return keys.get(input("  > ").strip(), DEFAULT_MODE)
        except EOFError:
            return DEFAULT_MODE
    with silent_keyboard():
        while True:
            if not select.select([sys.stdin], [], [], 0.2)[0]:
                continue
            key = os.read(sys.stdin.fileno(), 1)
            if key in (b"\n", b"\r", b""):
                return DEFAULT_MODE
            if key == b"\x03":
                raise KeyboardInterrupt
            chosen = keys.get(key.decode("utf-8", "ignore"))
            if chosen:
                return chosen


# --- The show ---------------------------------------------------------------

@dataclass
class Recap:
    """What an act leaves behind for its summary screen (cells and notes are markup)."""

    rows: list[tuple[str, ...]]    # what happened, one moment per row
    notes: list[tuple[str, str]]   # (heading, explanation)


class Show:
    """Everything on screen, plus the clock that paces it.

    Without a `live` display the show still runs, instantly: that rehearsal
    is how the real run knows its total length for the progress bar.
    """

    def __init__(self, console: Console, speed: float = 1.0, total: float = 0.0,
                 stops: bool = False, stepping: bool = False) -> None:
        self.console = console
        self.speed = speed
        self.total = total
        self.stops = stops        # stop on a summary after each act and wait for Enter
        self.stepping = stepping  # wait for Enter at every step, not just between acts
        self.steps = 0            # how many steps the whole show takes
        self.step_no = 0          # how many have gone by
        self.recap: Recap | None = None
        self.following = ""
        self.live: Live | None = None
        self.elapsed = 0.0

        self.act = INTRO
        self.title = ""
        self.narration = ""
        self.visual: Callable[[int, int], RenderableType] = lambda width, height: Text()
        self.show_log = True
        self.clock = 0.0
        self.allowed = 0
        self.blocked = 0
        self.verdict: tuple[bool, str] | None = None
        self.hits: list[Hit] = []
        self.log: deque[Hit | str] = deque(maxlen=80)
        # (limiter, attack, logins let through, verdict, held?)
        self.results: list[tuple[str, str, str, str, bool]] = []

    # -- pacing --

    def beat(self, seconds: float) -> None:
        """Hold the current picture for `seconds` of show time."""
        seconds *= PACE
        start = self.elapsed
        self.step_no += 1
        if self.live is not None and self.stepping:
            live = self.live
            self.elapsed = start + seconds
            wait_for_enter(lambda: live.update(self.render(), refresh=True), flush=False)
            return
        if self.live is not None:
            deadline = time.monotonic() + seconds / self.speed
            while True:
                left = deadline - time.monotonic()
                self.elapsed = start + seconds - max(left, 0.0) * self.speed
                self.live.update(self.render(), refresh=True)
                if left <= 0:
                    break
                time.sleep(min(left, 1 / FPS))
        self.elapsed = start + seconds

    def review(self, recap: Recap, following: str) -> None:
        """Hold the act's summary on screen until the viewer presses Enter."""
        if self.live is None or not self.stops:
            return
        live = self.live
        self.recap, self.following = recap, following
        wait_for_enter(lambda: live.update(self.render(), refresh=True))
        self.recap = None

    # -- what the acts call --

    def scene(self, act: int, title: str, visual: Callable[[int, int], RenderableType], *, log: bool = True) -> None:
        self.act, self.title, self.visual, self.show_log = act, title, visual, log
        self.narration = ""
        self.reset_score()
        self.log.clear()

    def reset_score(self) -> None:
        self.allowed = self.blocked = 0
        self.verdict = None
        self.hits = []

    def say(self, markup: str, seconds: float) -> None:
        self.narration = markup
        self.beat(seconds)

    def mark(self, label: str) -> None:
        self.log.append(label)

    def request(self, now: float, label: str, ok: bool, pause: float) -> None:
        hit = Hit(now, label, ok)
        self.clock = now
        self.hits.append(hit)
        self.log.append(hit)
        if ok:
            self.allowed += 1
        else:
            self.blocked += 1
        self.beat(pause)

    def judge(self, ok: bool, text: str, *, row: tuple[str, str, str, str]) -> None:
        self.verdict = (ok, text)
        self.results.append((*row, ok))

    # -- drawing --

    def render(self) -> RenderableType:
        root = Layout()
        header = Layout(self.header(), size=3)
        if self.act == FINALE:
            root.split_column(header, Layout(Align.center(self.summary(), vertical="middle")))
            return root
        if self.recap is not None:
            root.split_column(
                header,
                Layout(Align.center(self.review_panel(self.recap), vertical="middle")),
                Layout(self.prompt(), size=1),
            )
            return root

        stage = Layout()
        visual = Panel(Sized(self.visual), border_style="dim", padding=(0, 1))
        if self.show_log:
            width = self.console.width
            log_width = 58 if width >= 116 else 40 if width >= 92 else 32
            log = Panel(
                LogView(self.log),
                title="LIVE REQUESTS",
                title_align="left",
                border_style="dim",
                padding=(0, 1),
            )
            stage.split_row(Layout(visual), Layout(log, size=log_width))
        else:
            stage.update(visual)

        rows = [header, Layout(self.narrator(), size=5), stage]
        if self.act != INTRO:
            rows.append(Layout(self.footer(), size=3))
        root.split_column(*rows)
        return root

    def header(self) -> RenderableType:
        top = Table.grid(expand=True)
        top.add_column()
        top.add_column(justify="right")
        top.add_row(
            Text(f" {TITLE}", style="bold"),
            Text(f"rule: max {LIMIT} logins per minute, per user ", style="dim"),
        )

        steps = Text(" ")
        for number, name in enumerate(ACT_NAMES, 1):
            if number < self.act:
                steps.append(f" ✓ {name} ", style="green")
            elif number == self.act:
                steps.append(f" {number} {name} ", style=f"bold black on {ACCENT}")
            else:
                steps.append(f" {number} {name} ", style="dim")
            steps.append(" ")

        progress = Table.grid(expand=True, padding=(0, 1))
        progress.add_column(ratio=1)
        progress.add_column(no_wrap=True)
        if self.stepping:
            done, whole = float(self.step_no), float(self.steps or 1)
            readout = f"step {self.step_no} / {self.steps}"
        else:
            done, whole = self.elapsed, self.total or 1
            readout = f"{mmss(self.elapsed / self.speed)} / {mmss(self.total / self.speed)}"
        progress.add_row(
            ProgressBar(total=whole, completed=done, complete_style=ACCENT, finished_style=ACCENT),
            Text(readout, style="dim"),
        )
        return Group(top, steps, Padding(progress, (0, 1)))

    def narrator(self) -> RenderableType:
        title = self.title if self.act == INTRO else f"ACT {self.act} · {self.title}"
        return Panel(
            Align.center(Text.from_markup(self.narration, justify="center"), vertical="middle"),
            title=f"[bold {ACCENT}]{title}[/]",
            title_align="left",
            border_style=ACCENT,
            padding=(0, 2),
        )

    def footer(self) -> RenderableType:
        grid = Table.grid(expand=True, padding=(0, 2))
        grid.add_column(no_wrap=True)
        grid.add_column(no_wrap=True)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")

        allowed = Text("✓ allowed ", style=OK)
        allowed.append(f"{self.allowed:<3}", style=f"bold {OK}")
        blocked = Text("✗ blocked ", style=BAD)
        blocked.append(f"{self.blocked:<3}", style=f"bold {BAD}")

        if self.verdict is None:
            waiting = "ENTER for the next step" if self.stepping else "watching…"
            verdict, border = Text(f"│ {waiting}", style="dim"), "dim"
        else:
            ok, text = self.verdict
            border = OK if ok else BAD
            verdict = Text("│ ", style="dim")
            verdict.append(f"{'✓' if ok else '✗'} {text}", style=f"bold {border}")
        grid.add_row(allowed, blocked, verdict)
        return Panel(grid, border_style=border, padding=(0, 1))

    def review_panel(self, recap: Recap) -> RenderableType:
        ok, text = self.verdict or (True, "")
        color = OK if ok else BAD

        happened = Table.grid(padding=(0, 2))
        for row in recap.rows:
            happened.add_row(*(Text.from_markup(cell) for cell in row))

        body = Table.grid(padding=(0, 2))
        body.add_column(style="bold", no_wrap=True)
        body.add_column()
        body.add_row("RESULT", Text(f"{'✓' if ok else '✗'} {text}", style=f"bold {color}"))
        body.add_row()
        body.add_row("WHAT HAPPENED", happened)
        for heading, note in recap.notes:
            body.add_row()
            body.add_row(heading, Text.from_markup(note))
        width, height = self.console.size
        return Panel(
            body,
            title=f"[bold {ACCENT}]ACT {self.act} SUMMARY · {self.title}[/]",
            title_align="left",
            border_style=color,
            padding=(1 if height >= 28 else 0, 2),
            width=min(width, 100),
        )

    def prompt(self) -> RenderableType:
        lit = int(time.monotonic() * 2) % 2 == 0
        line = Text(" ")
        line.append(" ENTER ", style=f"bold black on {ACCENT}" if lit else f"bold {ACCENT}")
        line.append(f"  next: {self.following}", style="bold")
        line.append("     Ctrl+C to quit", style="dim")
        return line

    def summary(self) -> RenderableType:
        table = Table(box=box.SIMPLE_HEAD, header_style="bold dim", show_edge=False, pad_edge=False)
        table.add_column("Limiter")
        table.add_column("Attack")
        table.add_column("Let through", justify="right")
        table.add_column("")
        for limiter, attack, through, verdict, ok in self.results:
            color = OK if ok else BAD
            table.add_row(
                Text(limiter, style="bold"),
                attack,
                Text(through, style=color),
                Text(f"{'✓' if ok else '✗'} {verdict}", style=f"bold {color}"),
            )

        answer = Text.from_markup(
            f"[bold]Interview answer[/]  Token bucket per user, state in shared Redis,\n"
            f"reject with [bold {BAD}]HTTP 429 Too Many Requests[/].\n"
        )
        scale = Text(
            f"Demo scale: {LIMIT} logins per minute. At 100 per minute the boundary\n"
            f"trap lets 200 logins through in about 2 seconds.",
            style="dim",
        )
        return Panel(
            Group(table, Text(), answer, scale),
            title=f"[bold {ACCENT}]WHAT WE SAW[/]  [dim]rule: {LIMIT} logins per minute[/]",
            title_align="left",
            border_style=ACCENT,
            padding=(1, 2),
            expand=False,
        )


# --- Views: the picture each act draws ----------------------------------------

def cast_view(width: int, height: int) -> RenderableType:
    def actor(name: str, role: str, color: str) -> Panel:
        return Panel(Text(role, justify="center"), title=f"[bold]{name}[/]", border_style=color, width=20)

    arrow = Text("\n──▶", style="bold")
    grid = Table.grid(padding=(0, 1))
    for _ in range(5):
        grid.add_column()
    grid.add_row(
        actor("ATTACKER", "guesses alice's\npassword", BAD),
        arrow,
        actor("RATE LIMITER", f"max {LIMIT} logins\nper minute", ACCENT),
        arrow,
        actor("LOGIN API", "checks the\npassword", OK),
    )
    over = Text("│\n▼\nover the limit?\n", justify="center", style="dim")
    over.append("429 Too Many Requests", style=f"bold {BAD}")
    grid.add_row("", "", over, "", "")
    return Align.center(grid, vertical="middle", height=height)


@dataclass
class FixedWindowView:
    show: Show
    limiter: FixedWindow
    through: str
    note: str = ""

    def __call__(self, width: int, height: int) -> RenderableType:
        fw = self.limiter
        counter = slots(fw.count, fw.limit)
        counter.append(f"   {fw.count} / {fw.limit}", style="bold")
        if self.note:
            counter.append(f"  {self.note}", style="bold yellow")
        elif fw.count >= fw.limit:
            counter.append("  FULL", style="bold yellow")
        return stack(
            [
                [section("COUNTER", "wiped when a new minute starts"), counter],
                timeline_block(self.show, width),
                [section(self.through, f"rule: {LIMIT}/minute"), through_bar(self.show.allowed, width)],
            ],
            height,
        )


@dataclass
class TokenBucketView:
    """A bucket of balls (tokens): a login that finds a ball takes it and
    passes; a login that finds the bucket empty is thrown out."""

    show: Show
    bucket: TokenBucket
    burst: int = 0  # logins let through by the two bursts
    last: tuple[int, bool] | None = None  # (attempt, allowed?) of the latest login

    def __call__(self, width: int, height: int) -> RenderableType:
        bucket = self.bucket
        whole = int(bucket.tokens + EPS)
        every = round(1 / bucket.refill_per_sec)
        per_layer = bucket.capacity // 2
        lane = " " * 10  # room left of the bucket for the arriving login

        def layer(balls: int) -> Text:
            row = Text("│ ", style="bold")
            row.append_text(slots(balls, per_layer, on="●", off="·", style="yellow"))
            row.append(" │", style="bold")
            return row

        head = section("BUCKET", "1 login takes 1 ball")
        head.append(f"   clock {mmss(self.show.clock)}", style=f"bold {ACCENT}")

        # The tap above the opening: fills up, then drops one ball in.
        tap = Text(lane + "   ")
        if whole >= bucket.capacity:
            tap.append("░" * every + "  bucket is full", style="dim")
        else:
            done = round((bucket.tokens - whole) * every)
            tap.append("█" * done, style="yellow")
            tap.append("░" * (every - done) + f"  +1 ball every {every} s", style="dim")

        top = Text(lane)
        if self.last:
            attempt, ok = self.last
            arriving = f"#{attempt:02d} ──{'▶' if ok else '✗'}"
            top = Text(f"{arriving:>9} ", style=f"bold {OK if ok else BAD}")
        top.append_text(layer(max(0, whole - per_layer)))
        if self.last and self.last[1]:
            top.append(" ──▶ ", style=OK)
            top.append("●", style="yellow")
            top.append(" ✓ 200 OK", style=f"bold {OK}")
        elif self.last:
            top.append(" ✗ 429 no ball left", style=f"bold {BAD}")

        bottom = Text(lane)
        bottom.append_text(layer(min(whole, per_layer)))
        base = Text(lane)
        base.append("╰" + "─" * (2 * per_layer + 1) + "╯", style="bold")
        if whole:
            base.append(f"  {whole} / {bucket.capacity} balls", style="bold")
        else:
            base.append("  EMPTY", style="bold yellow")

        took = Text("took a ball  ", style="dim")
        took.append("●" * self.show.allowed, style=OK)
        took.append(f" {self.show.allowed}", style=f"bold {OK}")
        thrown = Text("thrown out   ", style="dim")
        thrown.append("✗" * self.show.blocked, style=BAD)
        thrown.append(f" {self.show.blocked}", style=f"bold {BAD}")

        fixed = Text("fixed window  ", style="dim")
        fixed.append_text(through_bar(2 * LIMIT))
        ours = Text("token bucket  ", style="bold")
        ours.append_text(through_bar(self.burst))

        blocks: list[list[RenderableType]] = [[head, tap, top, bottom, base, took, thrown]]
        compare = [section("SAME ATTACK", "let through in the first 2 s"), fixed, ours]
        moments = timeline_block(self.show, width)
        if height >= len(blocks[0]) + len(moments) + len(compare) + 2:
            blocks.append(moments)
        blocks.append(compare)
        return stack(blocks, height)


@dataclass
class ServersView:
    show: Show
    local: list[FixedWindow]
    shared: FixedWindow | None = None
    last: tuple[int, bool] | None = None  # (server, allowed?) of the latest request

    def __call__(self, width: int, height: int) -> RenderableType:
        gap = "  " if width >= 43 else " "

        def tiles(servers: range) -> list[RenderableType]:
            names, counts = Text(), Text()
            for position, server in enumerate(servers):
                if position:
                    names.append(gap)
                    counts.append(gap)
                style = "bold"
                if self.last and self.last[0] == server:
                    style = f"bold white on {'green' if self.last[1] else 'red'}"
                names.append(f" srv{server + 1:02d} ", style=style)
                seen = "→redis" if self.shared else f"{self.local[server].count}/{LIMIT}"
                counts.append(f"{seen:^7}", style="dim" if self.shared else "")
            return [names, counts]

        if self.shared:
            where = Text("REDIS", style=f"bold {BAD}")
            where.append("  rate:login:alice  ", style="dim")
            where.append_text(slots(self.shared.count, LIMIT, sep=""))
            where.append(f" {self.shared.count}/{LIMIT}", style="bold")
        else:
            where = section("COUNTER", "one per server, in its own memory")

        half = SERVERS // 2
        return stack(
            [
                [Text(f"ATTACKER ─▶ LOAD BALANCER ─▶ {SERVERS} SERVERS", style="dim")],
                tiles(range(half)),
                tiles(range(half, SERVERS)),
                [where],
                [section("LOGINS LET THROUGH", f"rule: {LIMIT}/minute"), through_bar(self.show.allowed, width)],
            ],
            height,
        )


# --- Acts ---------------------------------------------------------------------

def intro(show: Show) -> None:
    show.scene(INTRO, "THE PROBLEM", cast_view, log=False)
    show.say("Someone keeps guessing the password for [bold]alice@example.com[/].", 1.6)
    show.say(
        f"A [bold]rate limiter[/] stands guard: max {LIMIT} logins a minute,\n"
        f"then [bold {BAD}]429 Too Many Requests[/]. Can it be tricked?",
        2.0,
    )


def act_fixed_window(show: Show) -> Recap:
    limiter = FixedWindow()
    show.scene(1, "FIXED WINDOW COUNTER", FixedWindowView(show, limiter, "LET THROUGH THIS MINUTE"))
    show.clock = 8
    show.say(f"The guard counts logins per user.\nThe attacker tries [bold]{LIMIT + 1} passwords[/] in one minute.", 1.2)

    for attempt in range(1, LIMIT + 2):
        now = 7 + attempt
        show.request(now, f"{mmss(now)} #{attempt:02d}", limiter.allow(now), 0.24)

    show.judge(
        True,
        f"{LIMIT} allowed, #{LIMIT + 1} rejected with 429",
        row=("Fixed window", "steady, one minute", str(show.allowed), "holds"),
    )
    show.say(f"The first {LIMIT} pass. Number {LIMIT + 1} gets [bold {BAD}]429[/]. The guard works… so far.", 2.0)
    return Recap(
        rows=[
            (f"{mmss(8)} to {mmss(7 + LIMIT)}", f"{LIMIT} logins", f"[{OK}]{LIMIT} allowed[/]", f"counter 0 → {LIMIT}"),
            (mmss(8 + LIMIT), f"login #{LIMIT + 1}", f"[{BAD}]429 rejected[/]", "counter is full"),
        ],
        notes=[
            ("HOW IT WORKS", f"One counter per user. Every login adds 1. At {LIMIT} the guard "
                             "answers 429 until the minute ends, then the counter is wiped to 0."),
            ("WHY USE IT", "It is simple and cheap: one number per user, one comparison per login."),
            ("THE CATCH", "The whole allowance comes back in a single instant, at the minute "
                          "boundary. The next act attacks exactly that moment."),
        ],
    )


def act_boundary_trap(show: Show) -> Recap:
    limiter = FixedWindow()
    view = FixedWindowView(show, limiter, "LET THROUGH IN 2 SECONDS")
    show.scene(2, "THE BOUNDARY TRAP", view)

    first, second = WINDOW - 1, WINDOW + 1
    show.clock = first
    show.say(f"Same guard. The attacker waits for the [bold]last second[/]\nof the minute, then fires {LIMIT} logins.", 1.5)
    show.mark(f"{mmss(first)} · burst of {LIMIT}")
    for attempt in range(1, LIMIT + 1):
        show.request(first, f"{mmss(first)} #{attempt:02d}", limiter.allow(first), 0.08)

    show.clock = WINDOW
    limiter.roll(WINDOW)
    view.note = "RESET → 0"
    show.mark(f"{mmss(WINDOW)} · counter reset")
    show.say(f"[bold yellow]{mmss(WINDOW)}[/], a new minute starts. The counter is wiped back to [bold]0[/].", 1.8)

    view.note = ""
    show.mark(f"{mmss(second)} · burst of {LIMIT}")
    for attempt in range(LIMIT + 1, 2 * LIMIT + 1):
        show.request(second, f"{mmss(second)} #{attempt:02d}", limiter.allow(second), 0.08)

    show.judge(
        False,
        f"{show.allowed} logins in 2 s, double the limit",
        row=("Fixed window", "boundary burst", f"{show.allowed} in 2 s", "2× the limit"),
    )
    show.say(f"[bold {BAD}]{show.allowed} logins in 2 seconds.[/] The rule said {LIMIT} per minute.", 2.6)
    return Recap(
        rows=[
            (mmss(first), f"{LIMIT} logins", f"[{OK}]{LIMIT} allowed[/]", f"counter 0 → {LIMIT}"),
            (mmss(WINDOW), "new minute", "", "counter wiped to 0"),
            (mmss(second), f"{LIMIT} logins", f"[{OK}]{LIMIT} allowed[/]", f"counter 0 → {LIMIT}"),
        ],
        notes=[
            ("WHY IT BREAKS", "The counter only knows which minute it is in. It cannot see that "
                              "the two bursts were 2 seconds apart, so each looks legal on its own."),
            ("AT REAL SCALE", "With a typical limit of 100 per minute, the same trick lets "
                              "200 password guesses through in about 2 seconds."),
            ("REMEMBER", "A fixed window can let through up to [bold]2× the limit[/] around "
                         "every boundary."),
        ],
    )


def act_token_bucket(show: Show) -> Recap:
    bucket = TokenBucket()
    view = TokenBucketView(show, bucket)
    show.scene(3, "TOKEN BUCKET · SAME ATTACK", view)
    every = WINDOW // LIMIT

    def login(attempt: int, now: float, pause: float, *, burst: bool = False) -> None:
        ok = bucket.allow(now)
        view.last = (attempt, ok)
        view.burst += ok and burst
        show.request(now, f"{mmss(now)} #{attempt:02d}", ok, pause)

    first, second = WINDOW - 1, WINDOW + 1
    show.clock = first
    show.say(
        f"New guard: a bucket of [bold]{LIMIT} tokens[/] (the balls). Each login takes one out.\n"
        f"New ones drip in slowly: 1 every {every} seconds.",
        2.2,
    )
    show.mark(f"{mmss(first)} · burst of {LIMIT}")
    for attempt in range(1, LIMIT + 1):
        login(attempt, first, 0.11, burst=True)

    show.clock = second
    bucket.refill(second)
    view.last = None
    show.say("A new minute starts, but [bold]nothing resets[/].\nTwo seconds of drip: the bucket is still empty.", 1.5)
    show.mark(f"{mmss(second)} · burst of {LIMIT}")
    for attempt in range(LIMIT + 1, 2 * LIMIT + 1):
        login(attempt, second, 0.11, burst=True)

    show.narration = (
        "The attacker keeps hammering. A login passes only when\n"
        f"a new ball drops in: [bold]1 every {every} seconds[/]."
    )
    show.mark("keeps trying, 1 per second")
    for attempt in range(2 * LIMIT + 1, 2 * LIMIT + 2 * every + 1):
        login(attempt, second + attempt - 2 * LIMIT, 0.17)

    show.judge(
        True,
        f"burst capped at {LIMIT}, then 1 login per {every} s",
        row=("Token bucket", "boundary burst", f"{view.burst} in 2 s", "holds"),
    )
    show.say(f"Same attack: [bold {OK}]{view.burst} get through, not {2 * LIMIT}[/]. No reset, no loophole.", 2.2)

    trickle = show.hits[2 * LIMIT:]
    passed = [mmss(hit.clock) for hit in trickle if hit.ok]
    return Recap(
        rows=[
            (mmss(first), f"{LIMIT} logins", f"[{OK}]{LIMIT} allowed[/]", f"bucket {LIMIT} → 0"),
            (mmss(second), f"{LIMIT} logins", f"[{BAD}]{LIMIT} rejected[/]", "bucket still empty"),
            (f"next {len(trickle)} s", f"{len(trickle)} logins", f"[{OK}]{len(passed)} allowed[/]", "at " + " and ".join(passed)),
        ],
        notes=[
            ("HOW IT WORKS", f"The bucket holds up to {LIMIT} tokens. A login takes one; no token "
                             f"means 429. Tokens return one at a time, 1 every {every} s ({LIMIT} per minute)."),
            ("WHY IT HOLDS", "Nothing is ever wiped, so there is no special moment to aim for. "
                             "An empty bucket stays empty until tokens drip back."),
            ("WHAT TO STORE", "Two values per user: tokens left, and the time of the last refill."),
        ],
    )


def act_servers(show: Show) -> Recap:
    per_server = 3
    tries = SERVERS * per_server
    view = ServersView(show, [FixedWindow() for _ in range(SERVERS)])
    show.scene(4, f"{SERVERS} SERVERS", view)
    show.clock = 5

    def attack(limiter_for: Callable[[int], FixedWindow]) -> None:
        for attempt in range(tries):
            server = attempt % SERVERS
            ok = limiter_for(server).allow(show.clock)
            view.last = (server, ok)
            show.request(show.clock, f"→ srv{server + 1:02d}", ok, 0.05)

    show.say("Real apps run on many servers.\nHere each one keeps [bold]its own counter[/] in memory.", 1.4)
    show.mark("a counter per server")
    attack(lambda server: view.local[server])
    show.judge(
        False,
        f"{show.allowed} allowed, each server saw only {per_server}",
        row=("Counter per server", f"{tries} tries, any server", str(show.allowed), "3× the limit"),
    )
    show.say(f"Every server sees just {per_server} logins and says OK. Together: [bold {BAD}]{show.allowed} allowed[/].", 1.8)

    leaked = show.allowed
    show.reset_score()
    view.shared, view.last = FixedWindow(), None
    show.say("The fix: every server reads and writes [bold]one shared counter in Redis[/].", 1.4)
    show.mark("one counter in Redis")
    attack(lambda server: view.shared)
    show.judge(
        True,
        f"{show.allowed} allowed, {show.blocked} blocked, one shared limit",
        row=("Shared Redis counter", f"{tries} tries, any server", str(show.allowed), "holds"),
    )
    show.say(f"Whichever server answers, the count is the same: [bold {OK}]{show.allowed} allowed[/], {show.blocked} blocked.", 2.0)
    return Recap(
        rows=[
            ("Own counters", f"{tries} logins", f"[{BAD}]{leaked} allowed[/]", f"{per_server} seen per server"),
            ("Shared Redis", f"{tries} logins", f"[{OK}]{show.allowed} allowed[/]", f"{show.blocked} rejected"),
        ],
        notes=[
            ("WHY IT BREAKS", "The load balancer spreads logins across servers and each one counts "
                              f"only what it sees. The real limit becomes {LIMIT} × {SERVERS} servers = {LIMIT * SERVERS}."),
            ("THE FIX", "Keep the count in one place every server can reach. Redis is in memory "
                        "(fast), updates atomically, and expires old keys with a TTL."),
            ("REMEMBER", "Limiter state must be shared, or adding servers quietly raises the limit."),
        ],
    )


def finale(show: Show) -> None:
    show.act = FINALE
    show.beat(3.6)


ACTS = [act_fixed_window, act_boundary_trap, act_token_bucket, act_servers]


def main() -> int:
    parser = argparse.ArgumentParser(description="Live terminal demo: fixed window vs token bucket rate limiting.")
    parser.add_argument("acts", nargs="*", type=int, choices=range(1, len(ACTS) + 1), metavar="ACT",
                        help="acts to play, 1-4 (default: the full show)")
    parser.add_argument("--speed", type=float, default=None, help="playback speed, e.g. 0.5 or 2 (skips the menu)")
    parser.add_argument("--step", action="store_true", help="wait for Enter at every step (skips the menu)")
    parser.add_argument("--auto", action="store_true", help="don't stop for Enter at all")
    args = parser.parse_args()
    if args.speed is not None and args.speed <= 0:
        parser.error("--speed must be greater than 0")

    def play(show: Show) -> None:
        if not args.acts:
            intro(show)
        numbers = sorted(set(args.acts)) or list(range(1, len(ACTS) + 1))
        for number, after in zip(numbers, [*numbers[1:], None]):
            recap = ACTS[number - 1](show)
            show.review(recap, f"Act {after} · {ACT_NAMES[after - 1]}" if after else "the final scoreboard")
        finale(show)

    console = Console()
    rehearsal = Show(console)
    play(rehearsal)

    if not console.is_terminal:
        console.print(rehearsal.summary())
        return 0
    if console.width < MIN_WIDTH or console.height < MIN_HEIGHT:
        console.print(
            f"This demo needs a terminal of at least {MIN_WIDTH}×{MIN_HEIGHT} "
            f"(yours is {console.width}×{console.height}). Enlarge the window and run it again."
        )
        return 1

    stops = not args.auto and sys.stdin.isatty()
    if args.auto:
        mode = Mode("", "auto", "", args.speed or 1.0, False)
    elif args.step:
        mode = MODES[0]
    elif args.speed is not None:
        mode = Mode("", "custom", "", args.speed, False)
    elif stops:
        mode = choose_mode(console, rehearsal)
    else:
        mode = DEFAULT_MODE

    show = Show(console, speed=mode.speed, total=rehearsal.elapsed, stops=stops,
                stepping=mode.stepping and stops)
    show.steps = rehearsal.step_no
    try:
        with silent_keyboard(), Live(console=console, screen=True, auto_refresh=False) as live:
            show.live = live
            play(show)
    except KeyboardInterrupt:
        return 130
    console.print(show.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
