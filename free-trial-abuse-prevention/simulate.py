#!/usr/bin/env python3
"""
Free trial abuse prevention: a live terminal demo (about 80 seconds).

A streaming service wants to give one free trial per person. Ten signups
arrive: five honest people, and one person who comes back five times.
Four acts run the same ten signups past four different policies:
  1. One per IP      blocks a whole office, misses a mobile hotspot
  2. Fingerprint     better, until the same person opens another browser
  3. Risk score      weak signals added up, then grant or deny
  4. + Challenge     the uncertain middle is asked to prove itself

After each act the show stops on a summary of what just happened and
waits for Enter before it moves on.

It asks how you want to watch: step by step, normal, slow or fast.

Run:  python simulate.py              full show, Enter between acts
      python simulate.py --auto       no stops, plays straight through
      python simulate.py 1 3          only these acts
      python simulate.py --speed 0.5  half speed (2 = twice as fast)

Needs the 'rich' library (pip install rich) and a terminal of at least 80x24.
"""

from __future__ import annotations

import argparse
import os
import select
import sys
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
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

CHALLENGE_AT, DENY_AT = 40, 70  # risk score bands used in acts 3 and 4
BURST = 3                       # signups from one network in an hour before it counts
MAX_RISK = 100                  # the score is capped, so the meter always fits

TITLE = "FREE TRIAL · ONE PER PERSON"

MIN_WIDTH, MIN_HEIGHT = 80, 24
FPS = 24
PACE = 1.0  # stretches every pause in the show; raise it to slow everything down

OK = "bright_green"
BAD = "bright_red"
WARN = "yellow"
ACCENT = "cyan"

ACT_NAMES = ["One per IP", "Fingerprint", "Risk score", "+ Challenge"]
INTRO, FINALE = 0, len(ACT_NAMES) + 1


# --- The signups ------------------------------------------------------------

@dataclass(frozen=True)
class Signup:
    """One attempt to start a free trial, with everything the service can see.

    `repeat` is the ground truth: this person has already had a trial. The
    policies never look at it; it is only used to score their decisions.
    """

    handle: str
    repeat: bool
    ip: str
    where: str        # how they are connected, shown next to the IP
    network: str      # the address range the IP sits in
    device: str       # what fingerprinting makes of their browser
    card: str
    email: str        # how old the email address is
    fresh: bool       # the email was created today
    velocity: int     # signups from this network in the last hour
    odd: bool         # behaves like accounts that abused trials before
    verifies: bool    # their card would survive a verification charge

    @property
    def mark(self) -> str:
        return f"{self.handle} ↺" if self.repeat else self.handle


REPEATS = 4  # free2 to free5: signups by someone who already had a trial

HOME, OFFICE, BROADBAND = "198.51.100.23", "203.0.113.7", "192.0.2.44"
LAPTOP = "mac/chrome·7c3f"   # Priya's laptop, shared with Vikram at home
PHONE_CARD = "••6650"        # the one real card the repeat visitor has

SIGNUPS = [
    Signup("Priya", False, HOME, "home wifi", "198.51.100.x",
           LAPTOP, "••4421", "2 years", False, 1, False, True),
    Signup("Rahul", False, OFFICE, "office wifi", "203.0.113.x",
           "win/edge·1a90", "••8812", "3 years", False, 1, False, True),
    Signup("Sneha", False, OFFICE, "same office", "203.0.113.x",
           "mac/safari·55e2", "••3390", "1 year", False, 1, False, True),
    Signup("Arjun", False, OFFICE, "same office", "203.0.113.x",
           "win/chrome·b471", "••7705", "4 years", False, 1, False, True),
    Signup("free1", False, BROADBAND, "home broadband", "192.0.2.x",
           "win/firefox·9e21", PHONE_CARD, "today", True, 1, False, True),
    Signup("free2", True, BROADBAND, "same line", "192.0.2.x",
           "win/firefox·9e21", PHONE_CARD, "today", True, 2, False, True),
    Signup("free3", True, BROADBAND, "same line", "192.0.2.x",
           "win/chrome·02bb", PHONE_CARD, "today", True, 3, False, True),
    Signup("free4", True, "100.64.8.19", "mobile hotspot", "100.64.x.x",
           "and/chrome·4f70", PHONE_CARD, "today", True, 4, False, True),
    Signup("free5", True, "100.64.21.5", "hotspot again", "100.64.x.x",
           "and/firefox·88c1", "prepaid ••0002", "today", True, 5, True, False),
    Signup("Vikram", False, HOME, "family wifi", "198.51.100.x",
           LAPTOP, "••9087", "2 years", False, 1, False, True),
]


class Ledger:
    """What the service has seen on the trials it has already handed out."""

    def __init__(self) -> None:
        self.ips: list[str] = []
        self.devices: list[str] = []
        self.cards: list[str] = []

    def record(self, s: Signup) -> None:
        for seen, value in ((self.ips, s.ip), (self.devices, s.device), (self.cards, s.card)):
            if value not in seen:
                seen.append(value)


def risk_signals(s: Signup, ledger: Ledger) -> list[tuple[int, str]]:
    """Every weak signal this signup trips, with what each one is worth."""
    signals = []
    if s.card in ledger.cards:
        signals.append((45, "card already used a trial"))
    if s.device in ledger.devices:
        signals.append((40, "device already used a trial"))
    if s.velocity >= BURST:
        signals.append((25, f"{s.velocity} signups on this network/h"))
    if s.fresh:
        signals.append((20, "email created today"))
    if s.ip in ledger.ips:
        signals.append((15, "IP already used a trial"))
    if s.odd:
        signals.append((15, "behaves like past abuse"))
    return signals


def band(score: int) -> str:
    return "grant" if score < CHALLENGE_AT else "challenge" if score < DENY_AT else "deny"


# --- Drawing helpers --------------------------------------------------------

def mmss(seconds: float) -> str:
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def section(title: str, hint: str = "") -> Text:
    text = Text(title, style="bold")
    if hint:
        text.append(f"  {hint}", style="dim")
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
class Op:
    """One decision, as it appears in the log."""

    who: str
    text: str
    good: bool | None = None  # green or red; None leaves the line neutral
    repeat: bool = False


class LogView:
    """Scrolling list of the decisions, newest at the bottom."""

    def __init__(self, entries: deque[Op | str]) -> None:
        self.entries = entries

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = options.max_width
        visible = list(self.entries)[-(options.height or 10):]
        for i, entry in enumerate(visible):
            if isinstance(entry, str):
                yield Text(f"{' ' + entry + ' ':─^{width}}", style="dim")
                continue
            newest = i == len(visible) - 1
            line = Text(f"{entry.who:<8}", style="magenta" if entry.repeat else ACCENT)
            color = "" if entry.good is None else OK if entry.good else BAD
            line.append(entry.text, style=f"bold {color}" if newest else color)
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
        self.blocked = 0   # honest people refused a trial
        self.leaked = 0    # repeat signups handed another trial
        self.verdict: tuple[bool, str] | None = None
        self.log: deque[Op | str] = deque(maxlen=80)
        # (policy, honest blocked, repeats allowed, verdict, held?)
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
        self.blocked = self.leaked = 0
        self.verdict = None
        self.log.clear()

    def say(self, markup: str, seconds: float) -> None:
        self.narration = markup
        self.beat(seconds)

    def mark(self, label: str) -> None:
        self.log.append(label)

    def decide(self, s: Signup, granted: bool, text: str) -> None:
        """Record one decision and score it against who the person really is."""
        if granted and s.repeat:
            self.leaked += 1
        if not granted and not s.repeat:
            self.blocked += 1
        self.log.append(Op(s.mark, text, granted, s.repeat))

    def judge(self, ok: bool, text: str, *, policy: str, short: str) -> None:
        """`text` goes under the act, `short` into the final scoreboard."""
        self.verdict = (ok, text)
        self.results.append((policy, str(self.blocked), str(self.leaked), short, ok))

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
            log_width = 40 if self.console.width >= 100 else 32
            log = Panel(
                LogView(self.log),
                title="DECISIONS",
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
            Text("10 signups · 6 entitled, 4 repeats ", style="dim"),
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

        blocked = Text("✗ honest blocked ", style=BAD if self.blocked else "dim")
        blocked.append(f"{self.blocked:<2}", style=f"bold {BAD}" if self.blocked else "bold dim")
        leaked = Text("✗ repeats let in ", style=BAD if self.leaked else "dim")
        leaked.append(f"{self.leaked:<2}", style=f"bold {BAD}" if self.leaked else "bold dim")

        if self.verdict is None:
            waiting = "ENTER for the next step" if self.stepping else "watching…"
            verdict, border = Text(f"│ {waiting}", style="dim"), "dim"
        else:
            ok, text = self.verdict
            border = OK if ok else BAD
            verdict = Text("│ ", style="dim")
            verdict.append(f"{'✓' if ok else '✗'} {text}", style=f"bold {border}")
        grid.add_row(blocked, leaked, verdict)
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
        table.add_column("Policy")
        table.add_column("Honest blocked", justify="right")
        table.add_column("Repeats let in", justify="right")
        table.add_column("")
        for policy, blocked, leaked, verdict, ok in self.results:
            color = OK if ok else BAD
            table.add_row(
                Text(policy, style="bold"),
                Text(blocked, style=OK if blocked == "0" else BAD),
                Text(leaked, style=OK if leaked == "0" else BAD),
                Text(f"{'✓' if ok else '✗'} {verdict}", style=f"bold {color}"),
            )

        answer = Text.from_markup(
            "[bold]Interview answer[/]  No single identifier proves who someone is.\n"
            "Add up weak signals into a risk score, then grant, challenge or deny.\n"
        )
        aim = Text(
            "The question is not \"is this the same person?\" but\n"
            "\"how likely is this signup to be abusing the trial?\"",
            style="dim",
        )
        return Panel(
            Group(table, Text(), answer, aim),
            title=f"[bold {ACCENT}]WHAT WE SAW[/]  [dim]the same 10 signups, four policies[/]",
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
        actor("SIGNUP", "an email and\na password", ACCENT),
        arrow,
        actor("THE CHECK", "has this person\nhad a trial?", WARN),
        arrow,
        actor("FREE TRIAL", "30 days,\none per person", OK),
    )
    both = Text("\ntoo strict: honest people blocked\ntoo loose: the same person, again", justify="center", style="dim")
    grid.add_row("", "", both, "", "")
    return Align.center(grid, vertical="middle", height=height)


def meter(score: int, width: int) -> list[Text]:
    """A risk bar coloured by band, with the band names underneath."""
    track = max(22, min(34, width - 10))
    bar = Text("  ")
    for i in range(track):
        at = (i + 1) / track * 100
        zone = OK if at <= CHALLENGE_AT else WARN if at <= DENY_AT else BAD
        bar.append("█" if at <= score else "░", style=zone if at <= score else f"dim {zone}")
    bar.append(f"  {score}", style=f"bold {OK if score < CHALLENGE_AT else WARN if score < DENY_AT else BAD}")

    labels = Text("  ")
    for name, start in (("grant", 0), ("challenge", CHALLENGE_AT), ("deny", DENY_AT)):
        at = 2 + round(start / 100 * track)
        labels.pad_right(max(0, at - labels.cell_len))
        style = "bold" if band(score) == name else "dim"
        labels.append(name[: max(4, track - labels.cell_len)], style=style)
    return [bar, labels]


@dataclass
class ReviewView:
    """One signup under review, drawn the way the current policy sees it."""

    show: Show
    rule: str
    ledger: Ledger
    mode: str                     # "ip", "device" or "score"
    signup: Signup | None = None
    signals: list[tuple[int, str]] = field(default_factory=list)
    score: int | None = None
    challenge: tuple[str, str] | None = None   # (what we asked for, how it went)
    outcome: tuple[str, bool] | None = None

    def card(self) -> list[RenderableType]:
        s = self.signup
        if s is None:
            return [section("SIGNUP", "waiting…")]
        head = Text("SIGNUP  ", style="bold")
        head.append(s.handle, style=f"bold {'magenta' if s.repeat else ACCENT}")
        head.append("  already had a trial ↺" if s.repeat else "  first time here", style="dim")
        lines = [
            ("IP", s.ip, s.where),
            ("device", s.device, ""),
            ("card", s.card, f"email {s.email}"),
        ]
        rows = []
        for label, value, note in lines:
            row = Text(f"  {label:<7} ", style="dim")
            hot = (self.mode == "ip" and label == "IP" and value in self.ledger.ips) or (
                self.mode == "device" and label == "device" and value in self.ledger.devices
            )
            row.append(value, style=f"bold {BAD}" if hot else "bold")
            if note:
                row.append(f"   {note}", style="dim")
            rows.append(row)
        return [head, *rows]

    def seen(self, width: int) -> list[RenderableType]:
        s = self.signup
        by_ip = self.mode == "ip"
        values = self.ledger.ips if by_ip else self.ledger.devices
        mine = (s.ip if by_ip else s.device) if s else None
        head = section("TRIALS GIVEN SO FAR", f"by {'IP address' if by_ip else 'device'}")
        if not values:
            return [head, Text("  nothing on file yet", style="dim")]
        rows, row = [], Text("  ")
        for value in values:
            chunk = f"{value}  "
            if row.cell_len + len(chunk) > width:
                rows.append(row)
                row = Text("  ")
            row.append(chunk, style=f"bold {BAD}" if value == mine else "dim")
        rows.append(row)
        return [head, *rows[-2:]]

    def risk(self, room: int) -> list[RenderableType]:
        """The signals that fired, trimmed to `room` lines in all."""
        head = Text("SIGNALS", style="bold")
        if not self.signals:
            head.append("   nothing stands out", style="dim")
            return [head]
        if len(self.signals) <= room - 1:
            shown, left = self.signals, 0
        else:
            shown, left = self.signals[: room - 2], len(self.signals) - (room - 2)
        rows: list[RenderableType] = [head]
        for points, label in shown:
            row = Text(f"  +{points:<3}", style=f"bold {WARN}")
            row.append(label, style="dim")
            rows.append(row)
        if left:
            rows.append(Text(f"  …and {left} more", style="dim"))
        return rows

    def __call__(self, width: int, height: int) -> RenderableType:
        rule = [Text.from_markup(f"[dim]RULE[/]  {self.rule}")]
        blocks: list[list[RenderableType]] = [rule, self.card()]

        if self.mode == "score":
            tail = (2 if self.score is not None else 0) + (2 if self.challenge else 0)
            tail += 1 if self.outcome else 0
            room = height - len(rule) - len(blocks[-1]) - tail
            if room >= 2:  # below that, the score and the decision matter more
                blocks.append(self.risk(room))
            if self.score is not None:
                blocks.append(meter(self.score, width))
        else:
            blocks.append(self.seen(width - 2))

        if self.challenge:
            asked, how = self.challenge
            lines = [section("CHALLENGE", asked)]
            lines.append(Text.from_markup(f"  {how}"))
            blocks.append(lines)

        if self.outcome:
            text, good = self.outcome
            line = Text("DECISION  ", style="bold")
            line.append(f"{'✓' if good else '✗'} {text}", style=f"bold {OK if good else BAD}")
            blocks.append([line])
        return stack(blocks, height)


# --- Acts ---------------------------------------------------------------------

def present(show: Show, view: ReviewView, s: Signup) -> None:
    """Put a signup on screen before the policy says anything about it."""
    view.signup, view.outcome, view.signals, view.score, view.challenge = s, None, [], None, None
    show.beat(0.3)


def intro(show: Show) -> None:
    show.scene(INTRO, "THE PROBLEM", cast_view, log=False)
    show.say("A streaming service gives everyone [bold]one free trial[/].\nNothing stops someone signing up again with a new email.", 2.1)
    show.say(
        "Ten signups are coming. Five honest people, and one person\n"
        "who comes back [bold]five times[/] — marked [magenta]↺[/] when they do.",
        2.6,
    )


def act_one_per_ip(show: Show) -> Recap:
    ledger = Ledger()
    view = ReviewView(show, "one free trial per IP address", ledger, "ip")
    show.scene(1, "ONE TRIAL PER IP ADDRESS", view)
    show.say("First try: remember the [bold]IP address[/] of every trial we give,\nand refuse a second one from the same address.", 2.1)

    for s in SIGNUPS:
        if s.handle == "free1":
            show.mark("one person, five signups")
        present(show, view, s)
        granted = s.ip not in ledger.ips
        view.outcome = ("trial granted", True) if granted else ("denied · this IP already had one", False)
        show.decide(s, granted, "✓ trial granted" if granted else "✗ denied · same IP")
        if granted:
            ledger.record(s)
        show.beat(0.55)
        if s.handle == "Sneha":
            show.say("Sneha works with Rahul. [bold]One office, one IP[/] —\nand she is refused a trial she never had.", 2.2)
        if s.handle == "free4":
            show.say("The same person switches to a [bold]mobile hotspot[/].\nNew IP, so the rule has nothing to match.", 2.2)

    show.judge(
        False,
        f"{show.blocked} honest people blocked, {show.leaked} repeats let in",
        policy="One per IP",
        short="wrong both ways",
    )
    show.say(f"[bold {BAD}]Wrong in both directions.[/] An IP is a place,\nnot a person: shared by many, changed in a second.", 2.1)
    return Recap(
        rows=[
            ("Sneha, Arjun", "share Rahul's IP", f"[{BAD}]2 blocked[/]", "honest"),
            ("Vikram", "Priya's home IP", f"[{BAD}]blocked[/]", "honest"),
            ("free2, free3", "same line", f"[{OK}]denied[/]", "correct"),
            ("free4, free5", "mobile hotspot", f"[{BAD}]granted[/]", "new IP"),
        ],
        notes=[
            ("WHY IT MISSES", "One public IP is shared by a household, an office, a university, "
                              "a hotel or a whole mobile network. Many people look like one."),
            ("WHY IT LEAKS", "Changing IP is easy: mobile data, a hotspot, a VPN, or just "
                             "waiting for the home router to get a new address."),
            ("STILL USEFUL", "An IP is a real signal, it is just not proof. Keep it as one "
                             "input among several, never as the whole rule."),
        ],
    )


def act_fingerprint(show: Show) -> Recap:
    ledger = Ledger()
    view = ReviewView(show, "one free trial per device fingerprint", ledger, "device")
    show.scene(2, "DEVICE FINGERPRINT", view)
    show.say("Better idea: fingerprint the [bold]device[/] — browser, OS,\nscreen size, language, time zone — and match on that.", 2.2)

    for s in SIGNUPS:
        if s.handle == "free1":
            show.mark("one person, five signups")
        present(show, view, s)
        granted = s.device not in ledger.devices
        view.outcome = ("trial granted", True) if granted else ("denied · this device already had one", False)
        show.decide(s, granted, "✓ trial granted" if granted else "✗ denied · device")
        if granted:
            ledger.record(s)
        show.beat(0.55)
        if s.handle == "Arjun":
            show.say("The office is fine now: three colleagues, three devices.\nThis already beats the IP rule.", 2.2)
        if s.handle == "free3":
            show.say("But the same person just opened [bold]another browser[/].\nDifferent fingerprint, so it looks like a new device.", 2.1)

    show.judge(
        False,
        f"{show.blocked} honest blocked, {show.leaked} repeats let in",
        policy="One per device",
        short="leaks 3 repeats",
    )
    show.say("Fewer honest people blocked, [bold]more abuse through[/].\nA fingerprint is a guess, and it is cheap to change.", 2.2)
    return Recap(
        rows=[
            ("the office", "three devices", f"[{OK}]granted[/]", "fixed"),
            ("free2", "same browser", f"[{OK}]denied[/]", "correct"),
            ("free3, free4, free5", "browser, then phone", f"[{BAD}]granted[/]", "new hash"),
            ("Vikram", "Priya's laptop", f"[{BAD}]blocked[/]", "honest"),
        ],
        notes=[
            ("WHAT IT IS", "A fingerprint is a hash of what the browser reveals: user agent, "
                           "OS, screen, language, time zone, fonts, canvas rendering."),
            ("WHY IT LEAKS", "Switch browser, use another device, open a private window, clear "
                             "storage, or run an anti-fingerprint extension, and it changes."),
            ("WHY IT BLOCKS", "Two people share one laptop at home and look like one person. "
                              "Two identical corporate machines can hash the same way."),
        ],
    )


def act_risk_score(show: Show) -> Recap:
    ledger = Ledger()
    view = ReviewView(show, f"add up the signals · deny at {CHALLENGE_AT}", ledger, "score")
    show.scene(3, "RISK SCORE", view)
    show.say("Stop hunting for one perfect identifier.\nCollect [bold]weak signals[/] and add up how suspicious a signup looks.", 2.2)

    for s in SIGNUPS:
        if s.handle == "free1":
            show.mark("one person, five signups")
        present(show, view, s)
        view.signals = risk_signals(s, ledger)
        show.beat(0.3)
        view.score = score = min(MAX_RISK, sum(points for points, _ in view.signals))
        granted = score < CHALLENGE_AT
        view.outcome = ("trial granted", True) if granted else (f"denied · risk {score}", False)
        show.decide(s, granted, "✓ trial granted" if granted else f"✗ denied · risk {score}")
        if granted:
            ledger.record(s)
        show.beat(0.5)
        if s.handle == "free2":
            show.say("Four signals at once: same device, same card, same IP,\nemail made today. The score [bold]maxes out[/] — denied.", 2.1)
        if s.handle == "free5":
            show.say("New device, new IP, new card, and still [bold]risk 60[/]:\nsignup speed, a fresh email, familiar behaviour.", 2.1)

    show.judge(
        False,
        f"all {REPEATS} repeats stopped, but {show.blocked} honest blocked",
        policy="Risk score",
        short="1 honest blocked",
    )
    show.say(f"Every repeat stopped. But Vikram scored [bold]55[/] on his wife's laptop\nand a hard deny gives him [bold {BAD}]no way out[/].", 2.1)
    return Recap(
        rows=[
            ("4 honest", "nothing much", "risk 0-15", f"[{OK}]granted[/]"),
            ("free2-free4", "card, device, IP", "risk 90-100", f"[{OK}]denied[/]"),
            ("free5", "all new, still odd", "risk 60", f"[{OK}]denied[/]"),
            ("Vikram", "Priya's laptop", "risk 55", f"[{BAD}]denied[/]"),
        ],
        notes=[
            ("THE IDEA", "No signal proves anything alone. Give each one a weight, add up the "
                         "ones that fire, and judge the total instead of any single fact."),
            ("WHY IT WORKS", "A repeat visitor can beat one signal cheaply, but beating six at "
                             "once costs a new device, a new network and a new payment method."),
            ("WHAT IS LEFT", "A middle band where the service is unsure. Denying it outright "
                             "turns every honest borderline user away, as it just did to Vikram."),
        ],
    )


def act_challenge(show: Show) -> Recap:
    ledger = Ledger()
    view = ReviewView(show, f"grant <{CHALLENGE_AT} · challenge <{DENY_AT} · deny {DENY_AT}+", ledger, "score")
    show.scene(4, "RISK SCORE + CHALLENGE", view)
    show.say(f"Last change: stop treating \"unsure\" as \"no\".\nBetween [bold]{CHALLENGE_AT}[/] and [bold]{DENY_AT}[/], ask the signup to [bold]prove itself[/].", 2.1)

    for s in SIGNUPS:
        if s.handle == "free1":
            show.mark("one person, five signups")
        present(show, view, s)
        view.signals = risk_signals(s, ledger)
        show.beat(0.3)
        view.score = score = min(MAX_RISK, sum(points for points, _ in view.signals))
        where = band(score)
        if where == "challenge":
            view.challenge = ("verify a payment method", f"[dim]{s.card} …[/]")
            show.beat(0.9)
            passed = s.verifies
            view.challenge = (
                "verify a payment method",
                f"[bold {OK}]{s.card} verified[/]" if passed else f"[bold {BAD}]{s.card} declined[/]",
            )
            granted = passed
            text = "challenge passed · granted" if passed else "challenge failed · denied"
            logged = "? verify → passed" if passed else "? verify → failed"
        else:
            granted = where == "grant"
            text = "trial granted" if granted else f"denied · risk {score}"
            logged = "✓ trial granted" if granted else f"✗ denied · risk {score}"
        view.outcome = (text, granted)
        show.decide(s, granted, logged)
        if granted:
            ledger.record(s)
        show.beat(0.5)
        if s.handle == "free5":
            show.say("Risk 60 lands in the middle, so we ask for a card.\nThe prepaid one is [bold]declined[/] — that is the answer.", 2.2)
        if s.handle == "Vikram":
            show.say("Vikram scores 55 and is asked the same question.\nHis own card verifies, and he [bold]gets his trial[/].", 2.2)

    show.judge(
        True,
        f"{show.blocked} honest blocked, {show.leaked} repeats let in",
        policy="Risk + challenge",
        short="holds both ways",
    )
    show.say(f"[bold {OK}]Nobody honest turned away, no repeat let through.[/]\nThe uncertain cases were asked, not judged.", 2.1)
    return Recap(
        rows=[
            (f"under {CHALLENGE_AT}", "5 signups", f"[{OK}]granted[/]", "no friction"),
            (f"{CHALLENGE_AT}-{DENY_AT - 1}", "free5 · prepaid", f"[{BAD}]declined[/]", "denied"),
            (f"{CHALLENGE_AT}-{DENY_AT - 1}", "Vikram · own card", f"[{OK}]verified[/]", "granted"),
            (f"{DENY_AT}+", "free2, free3, free4", f"[{OK}]denied[/]", "no challenge"),
        ],
        notes=[
            ("THE CHANGE", "Three outcomes instead of two. Low risk passes silently, high risk "
                           "is refused, and the middle is asked for something an abuser finds expensive."),
            ("WHY A CARD", "A second email or browser profile is free. A payment method that "
                           "has not already claimed a trial is not. Families share cards though, so it stays a signal, not a verdict."),
            ("REMEMBER", "Fraud work is probability, not proof. Ask \"how likely is this abuse?\" "
                         "and make the response fit the answer."),
        ],
    )


def finale(show: Show) -> None:
    show.act = FINALE
    show.beat(3.6)


ACTS = [act_one_per_ip, act_fingerprint, act_risk_score, act_challenge]


def main() -> int:
    parser = argparse.ArgumentParser(description="Live terminal demo: stopping free trial abuse.")
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
