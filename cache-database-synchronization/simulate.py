#!/usr/bin/env python3
"""
Cache and database synchronization: a live terminal demo (about 85 seconds).

An online store keeps prices in a database and a copy in a cache. Four acts
change one price from 100 to 200 and watch what customers are shown:
  1. The rule       update the database, then delete the cache key
  2. The race       a slow reader puts the old price back, for good
  3. TTL            the same race, but the stale entry expires after 60 s
  4. Lease token    the same race, and the stale write is refused

After each act the show stops on a summary of what just happened and
waits for Enter before it moves on.

Run:  python simulate.py              full show, Enter between acts
      python simulate.py --auto       no stops, plays straight through
      python simulate.py 2 4          only these acts
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

KEY = "price:42"
OLD_PRICE, NEW_PRICE = 100, 200
TTL = 60        # seconds a cache entry lives in act 3
CUSTOMERS = 6   # customers who ask for the price after the change

MIN_WIDTH, MIN_HEIGHT = 80, 24
FPS = 24
PACE = 1.0  # stretches every pause in the show; raise it to slow everything down

OK = "bright_green"
BAD = "bright_red"
ACCENT = "cyan"

READER, WRITER = "User A", "Admin"
ACTOR_STYLE = {READER: "cyan", WRITER: "magenta"}

ACT_NAMES = ["The rule", "The race", "TTL", "Lease token"]
INTRO, FINALE = 0, len(ACT_NAMES) + 1


# --- Database and cache -----------------------------------------------------

@dataclass
class Database:
    """The source of truth. Slow to ask, always right."""

    price: int = OLD_PRICE


@dataclass
class Entry:
    value: int
    expires: float | None = None  # clock second at which the entry vanishes


class Cache:
    """A fast copy of what the database said: get, set, delete."""

    def __init__(self) -> None:
        self.entries: dict[str, Entry] = {}

    def get(self, key: str, now: float) -> int | None:
        entry = self.entries.get(key)
        if entry and entry.expires is not None and now >= entry.expires:
            del self.entries[key]
            entry = None
        return entry.value if entry else None

    def set(self, key: str, value: int, now: float, ttl: float | None = None) -> None:
        self.entries[key] = Entry(value, now + ttl if ttl else None)

    def delete(self, key: str) -> bool:
        """True if there was something to delete."""
        return self.entries.pop(key, None) is not None


class LeaseCache(Cache):
    """A cache that hands out a lease token on every miss.

    A key can be filled only with its current token, and deleting the key
    cancels that token. So a value read before the delete is refused.
    """

    def __init__(self) -> None:
        super().__init__()
        self.leases: dict[str, int] = {}
        self.issued = 0

    def get_or_lease(self, key: str, now: float) -> tuple[int | None, int | None]:
        value = self.get(key, now)
        if value is not None:
            return value, None
        self.issued += 1
        self.leases[key] = self.issued
        return None, self.issued

    def set_with_lease(self, key: str, value: int, token: int, now: float) -> bool:
        if self.leases.get(key) != token:
            return False
        del self.leases[key]
        self.set(key, value, now)
        return True

    def delete(self, key: str) -> bool:
        self.leases.pop(key, None)
        return super().delete(key)


# --- Drawing helpers --------------------------------------------------------

def mmss(seconds: float) -> str:
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def rupees(price: int) -> str:
    return f"₹{price}"


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
    """One step somebody took, as it appears in the log."""

    actor: str
    text: str
    good: bool | None = None  # green or red; None leaves the line neutral


class LogView:
    """Scrolling list of every step, in the order it really happened."""

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
            line = Text(f"{entry.actor:<7} ", style=ACTOR_STYLE.get(entry.actor, ""))
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


def wait_for_enter(redraw: Callable[[], None]) -> None:
    """Block until Enter, redrawing meanwhile so the prompt blinks and resizes show."""
    if termios is None:
        redraw()
        sys.stdin.readline()
        return
    fd = sys.stdin.fileno()
    termios.tcflush(fd, termios.TCIFLUSH)  # keys hit while the act was playing don't count
    while True:
        redraw()
        if select.select([fd], [], [], 0.1)[0] and os.read(fd, 1) in (b"\n", b"\r", b""):
            return


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

    def __init__(self, console: Console, speed: float = 1.0, total: float = 0.0, stops: bool = False) -> None:
        self.console = console
        self.speed = speed
        self.total = total
        self.stops = stops  # stop on a summary after each act and wait for Enter
        self.recap: Recap | None = None
        self.following = ""
        self.live: Live | None = None
        self.elapsed = 0.0

        self.act = INTRO
        self.title = ""
        self.narration = ""
        self.visual: Callable[[int, int], RenderableType] = lambda width, height: Text()
        self.show_log = True
        self.clock = 0.0  # seconds inside the story, not on the wall
        self.right = 0
        self.wrong = 0
        self.verdict: tuple[bool, str] | None = None
        self.log: deque[Op | str] = deque(maxlen=80)
        # (strategy, situation, wrong prices served, verdict, held?)
        self.results: list[tuple[str, str, str, str, bool]] = []

    # -- pacing --

    def beat(self, seconds: float) -> None:
        """Hold the current picture for `seconds` of show time."""
        seconds *= PACE
        start = self.elapsed
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
        self.clock = 0.0
        self.right = self.wrong = 0
        self.verdict = None
        self.log.clear()

    def say(self, markup: str, seconds: float) -> None:
        self.narration = markup
        self.beat(seconds)

    def mark(self, label: str) -> None:
        self.log.append(label)

    def op(self, actor: str, text: str, good: bool | None = None) -> None:
        self.log.append(Op(actor, text, good))

    def serve(self, who: str, price: int, truth: int, *, hit: bool = True, when: str = "") -> None:
        """A customer is shown `price`; it counts as wrong if the database disagrees."""
        right = price == truth
        self.right += right
        self.wrong += not right
        if hit:
            self.op(who, f"{when}GET → {rupees(price)} {'✓' if right else '✗ wrong'}", right)

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
            log_width = 44 if width >= 100 else 34
            log = Panel(
                LogView(self.log),
                title="EVERY STEP, IN ORDER",
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
            Text(" CACHE + DATABASE · STAYING IN SYNC", style="bold"),
            Text(f"a price changes: {rupees(OLD_PRICE)} → {rupees(NEW_PRICE)} ", style="dim"),
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
        progress.add_row(
            ProgressBar(total=self.total or 1, completed=self.elapsed, complete_style=ACCENT, finished_style=ACCENT),
            Text(f"{mmss(self.elapsed / self.speed)} / {mmss(self.total / self.speed)}", style="dim"),
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

        right = Text("✓ right price ", style=OK)
        right.append(f"{self.right:<2}", style=f"bold {OK}")
        wrong = Text("✗ wrong price ", style=BAD)
        wrong.append(f"{self.wrong:<2}", style=f"bold {BAD}")

        if self.verdict is None:
            verdict, border = Text("│ watching…", style="dim"), "dim"
        else:
            ok, text = self.verdict
            border = OK if ok else BAD
            verdict = Text("│ ", style="dim")
            verdict.append(f"{'✓' if ok else '✗'} {text}", style=f"bold {border}")
        grid.add_row(right, wrong, verdict)
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
        table.add_column("Strategy")
        table.add_column("Situation")
        table.add_column("Wrong prices", justify="right")
        table.add_column("")
        for strategy, situation, wrong, verdict, ok in self.results:
            color = OK if ok else BAD
            table.add_row(
                Text(strategy, style="bold"),
                situation,
                Text(wrong, style=color),
                Text(f"{'✓' if ok else '✗'} {verdict}", style=f"bold {color}"),
            )

        answer = Text.from_markup(
            "[bold]Interview answer[/]  Update the database, then delete the cache key.\n"
            "Guard cache fills with a lease token, and keep a TTL as a safety net.\n"
        )
        origin = Text(
            "Leases come from Facebook's work on memcached, published in 2013.",
            style="dim",
        )
        return Panel(
            Group(table, Text(), answer, origin),
            title=f"[bold {ACCENT}]WHAT WE SAW[/]  [dim]price changed {rupees(OLD_PRICE)} → {rupees(NEW_PRICE)}[/]",
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
        actor("STORE APP", "needs the price\nof a product", ACCENT),
        arrow,
        actor("CACHE", "a fast copy,\ncan go stale", "yellow"),
        arrow,
        actor("DATABASE", "slow, but\nalways right", OK),
    )
    miss = Text("\non a miss: ask the\ndatabase, save here", justify="center", style="dim")
    grid.add_row("", "", miss, "", "")
    return Align.center(grid, vertical="middle", height=height)


@dataclass
class StoreView:
    """The database and the cache side by side, plus whoever is mid-request."""

    show: Show
    db: Database
    cache: Cache
    cache_note: str = ""                        # line under the cache box
    flight: tuple[str, str, str] | None = None  # (actor, what they hold, style)

    def __call__(self, width: int, height: int) -> RenderableType:
        show, truth = self.show, self.db.price
        entry = self.cache.entries.get(KEY)
        if entry and entry.expires is not None and show.clock >= entry.expires:
            entry = None

        db_inner, cache_inner, gap = 10, 24, "  "
        if entry is None:
            edge, content = "dim", Text(" empty", style="dim")
        elif entry.value == truth:
            edge, content = OK, Text(f" {rupees(entry.value)}", style=f"bold {OK}")
            content.append("  ✓ matches DB", style=OK)
        else:
            edge, content = BAD, Text(f" {rupees(entry.value)}", style=f"bold {BAD}")
            content.append("  ✗ STALE", style=f"bold {BAD}")
        content.pad_right(cache_inner - content.cell_len)

        labels = Text(f"{'DATABASE':<{db_inner + 2}}{gap}CACHE", style="bold")
        labels.append(f"  {KEY}", style="dim")
        top = Text(f"╭{'─' * db_inner}╮{gap}")
        top.append(f"╭{'─' * cache_inner}╮", style=edge)
        middle = Text("│")
        middle.append(f"{rupees(truth):^{db_inner}}", style="bold")
        middle.append(f"│{gap}")
        middle.append("│", style=edge)
        middle.append_text(content)
        middle.append("│", style=edge)
        bottom = Text(f"╰{'─' * db_inner}╯{gap}")
        bottom.append(f"╰{'─' * cache_inner}╯", style=edge)

        if entry and entry.expires is not None:
            note = f"expires in {int(entry.expires - show.clock)} s"
        elif self.cache_note:
            note = self.cache_note
        else:
            note = "never expires" if entry else ""
        under = Text(f"{'the truth':<{db_inner + 2}}{gap}{note}", style="dim")

        if self.flight:
            actor, holding, style = self.flight
            flying = Text(f"{actor}  ", style=ACTOR_STYLE.get(actor, "bold"))
            flying.append(holding, style=style)
        else:
            flying = Text("nobody", style="dim")

        right = Text("right price  ", style="dim")
        right.append("●" * show.right, style=OK)
        right.append(f" {show.right}", style=f"bold {OK}")
        wrong = Text("wrong price  ", style="dim")
        wrong.append("●" * show.wrong, style=BAD)
        wrong.append(f" {show.wrong}", style=f"bold {BAD}")

        served: list[RenderableType] = [right, wrong]
        if height >= 12:  # the heading is the first thing to go in a short terminal
            served.insert(0, section("CUSTOMERS SERVED"))
        return stack(
            [
                [labels, top, middle, bottom, under],
                [section("IN FLIGHT", "a request that is not finished"), flying],
                served,
            ],
            height,
        )


# --- Acts ---------------------------------------------------------------------

def intro(show: Show) -> None:
    show.scene(INTRO, "THE SETUP", cast_view, log=False)
    show.say("An online store looks up prices all day.\nThe database is the truth, but asking it is slow.", 2.2)
    show.say(
        "So it keeps a copy in a [bold]cache[/]. The hard part:\n"
        "what happens to the copy when the [bold]price changes[/]?",
        2.6,
    )


def act_rule(show: Show) -> Recap:
    db, cache = Database(), Cache()
    view = StoreView(show, db, cache)
    show.scene(1, "THE RULE", view)

    show.say("Reading a price: look in the [bold]cache[/] first.\nOnly on a miss, ask the database and save the answer.", 2.2)
    who = "Cust 1"
    cache.get(KEY, show.clock)
    show.op(who, "GET → miss")
    view.flight = (who, "looked in the cache: miss", "")
    show.say("Customer 1 asks for the price. The cache is empty: a [bold]cache miss[/].", 1.5)
    price = db.price
    show.op(who, f"DB read → {rupees(price)}")
    view.flight = (who, f"holding {rupees(price)} from the database", "")
    show.say(f"So the request reads the database and gets {rupees(price)}.", 1.4)
    cache.set(KEY, price, show.clock)
    show.op(who, f"SET {rupees(price)}", True)
    show.serve(who, price, db.price, hit=False)
    view.flight = None
    show.say(f"It saves {rupees(price)} in the cache, then answers the customer.", 1.5)

    show.narration = "The next customers are served straight from the cache. No database trip."
    for n in (2, 3):
        show.serve(f"Cust {n}", cache.get(KEY, show.clock), db.price)
        show.beat(0.8)

    show.say("Changing a price: [bold]update the database first[/],\nthen [bold]delete the cache key[/].", 2.2)
    db.price = NEW_PRICE
    show.op(WRITER, f"DB write {rupees(NEW_PRICE)}")
    view.flight = (WRITER, "updated the database", "")
    show.say(f"The admin sets the price to {rupees(NEW_PRICE)} in the database.\nFor an instant the cache still says {rupees(OLD_PRICE)}…", 1.8)
    cache.delete(KEY)
    show.op(WRITER, "DEL key ✓ removed", True)
    view.flight = None
    show.say(f"…so the admin deletes the key. The old {rupees(OLD_PRICE)} is gone.", 1.6)

    who = "Cust 4"
    show.narration = f"The next customer misses, reloads {rupees(NEW_PRICE)}\nfrom the database, and refills the cache."
    cache.get(KEY, show.clock)
    show.op(who, "GET → miss")
    view.flight = (who, "looked in the cache: miss", "")
    show.beat(0.8)
    price = db.price
    show.op(who, f"DB read → {rupees(price)}")
    view.flight = (who, f"holding {rupees(price)} from the database", "")
    show.beat(0.8)
    cache.set(KEY, price, show.clock)
    show.op(who, f"SET {rupees(price)}", True)
    show.serve(who, price, db.price, hit=False)
    view.flight = None
    show.beat(0.9)
    for n in (5, 6):
        show.serve(f"Cust {n}", cache.get(KEY, show.clock), db.price)
        show.beat(0.6)

    show.judge(
        True,
        "every customer saw the right price",
        row=("Update, then delete", "one step at a time", str(show.wrong), "holds"),
    )
    show.say("Every customer saw the right price.\nBut every step here happened [bold]one at a time[/]…", 2.4)
    return Recap(
        rows=[
            ("Cust 1", "cache miss", "reads DB, fills cache", f"[{OK}]{rupees(OLD_PRICE)}[/]"),
            ("Cust 2-3", "cache hit", "no database trip", f"[{OK}]{rupees(OLD_PRICE)}[/]"),
            ("Admin", "price change", f"DB → {rupees(NEW_PRICE)}, key deleted", ""),
            ("Cust 4-6", "miss, then hits", "cache refilled", f"[{OK}]{rupees(NEW_PRICE)}[/]"),
        ],
        notes=[
            ("READING", "Look in the cache first. On a miss, read the database, save the answer "
                        "in the cache and return it. This pattern is called cache-aside."),
            ("WRITING", "Update the database first, then delete the cache key. The next reader "
                        "misses and reloads the new value."),
            ("THE CATCH", "This run took one step at a time. Real requests overlap, and the next "
                          "act overlaps a slow read with a price change."),
        ],
    )


def act_race(show: Show) -> Recap:
    db, cache = Database(), Cache()
    view = StoreView(show, db, cache)
    show.scene(2, "THE RACE", view)

    show.say("Same rule, same code. But now a slow reader\nand a price change [bold]overlap[/].", 2.0)
    cache.get(KEY, show.clock)
    show.op(READER, "GET → miss")
    view.flight = (READER, "looked in the cache: miss", "")
    show.say("User A asks for the price. The cache is empty: a miss.", 1.5)
    held = db.price
    show.op(READER, f"DB read → {rupees(held)}")
    view.flight = (READER, f"holding {rupees(held)} from the database", "")
    show.say(f"A reads {rupees(held)} from the database…\nand then stalls for a moment, [bold]before saving it[/].", 2.0)

    db.price = NEW_PRICE
    show.op(WRITER, f"DB write {rupees(NEW_PRICE)}")
    view.flight = (READER, f"holding {rupees(held)} · now out of date", BAD)
    show.say(f"Right then, the admin changes the price to {rupees(NEW_PRICE)}.\nA is still holding {rupees(held)}.", 2.0)
    removed = cache.delete(KEY)
    show.op(WRITER, "DEL key ✓ removed" if removed else "DEL key: already empty")
    show.say("The admin deletes the cache key, as the rule says.\nBut the cache is [bold]already empty[/]: nothing happens.", 2.2)

    cache.set(KEY, held, show.clock)
    show.op(READER, f"SET {rupees(held)} ✗ stale", False)
    view.flight = (READER, f"wrote {rupees(held)} into the cache", BAD)
    show.say(f"Now A wakes up and saves its {rupees(held)}.\nThe old price is [bold {BAD}]back in the cache[/].", 2.2)

    view.flight = None
    show.narration = f"Every customer after that is served {rupees(held)}. The database says {rupees(NEW_PRICE)}."
    for n in range(1, CUSTOMERS + 1):
        show.serve(f"Cust {n}", cache.get(KEY, show.clock), db.price)
        show.beat(0.45)

    show.judge(
        False,
        f"cache stuck on {rupees(held)}, DB says {rupees(NEW_PRICE)}",
        row=("Update, then delete", "slow read + update", f"{show.wrong}, no end", "stale forever"),
    )
    show.say("Nobody will delete that key again.\nThe cache stays wrong [bold]until the next price change[/].", 2.6)
    return Recap(
        rows=[
            (READER, "cache miss", f"reads DB: {rupees(held)}", "then stalls"),
            (WRITER, f"DB → {rupees(NEW_PRICE)}", "deletes cache key", "nothing to delete"),
            (READER, "wakes up", f"caches {rupees(held)}", f"[{BAD}]stale[/]"),
            (f"Cust 1-{CUSTOMERS}", "cache hit", f"shown {rupees(held)}", f"[{BAD}]DB says {rupees(NEW_PRICE)}[/]"),
        ],
        notes=[
            ("WHY IT BREAKS", "The delete ran while the cache was still empty, so it removed "
                              "nothing. User A's write came after it, carrying a value read before the update."),
            ("HOW LONG", "For good. Nothing touches that key again until the next price change, "
                         "or until the cache happens to evict it."),
            ("REMEMBER", "\"Update the database, then delete the key\" is needed, but on its own "
                         "it loses to one slow reader."),
        ],
    )


def act_ttl(show: Show) -> Recap:
    db, cache = Database(), Cache()
    view = StoreView(show, db, cache)
    show.scene(3, f"TTL · EXPIRE AFTER {TTL} S", view)

    show.say(f"First fix: give every cache entry a [bold]time to live[/].\nAfter {TTL} seconds it deletes itself.", 2.2)

    show.narration = f"The same race plays out: A reads {rupees(OLD_PRICE)}, the admin updates\nand deletes, then A saves its old {rupees(OLD_PRICE)}."
    cache.get(KEY, show.clock)
    show.op(READER, "GET → miss")
    view.flight = (READER, "looked in the cache: miss", "")
    show.beat(0.8)
    held = db.price
    show.op(READER, f"DB read → {rupees(held)}")
    view.flight = (READER, f"holding {rupees(held)} from the database", "")
    show.beat(0.8)
    db.price = NEW_PRICE
    show.op(WRITER, f"DB write {rupees(NEW_PRICE)}")
    view.flight = (READER, f"holding {rupees(held)} · now out of date", BAD)
    show.beat(0.8)
    cache.delete(KEY)
    show.op(WRITER, "DEL key: already empty")
    show.beat(0.8)
    cache.set(KEY, held, show.clock, ttl=TTL)
    show.op(READER, f"SET {rupees(held)}, TTL {TTL} s", False)
    view.flight = None
    show.say(f"The stale {rupees(held)} is in the cache again.\nBut this time a [bold]clock is ticking[/].", 2.0)

    show.narration = "Customers keep arriving, one every 10 seconds.\nThey are all shown the old price."
    customer = 0
    for now in range(10, TTL, 10):
        customer += 1
        show.clock = now
        show.serve(f"Cust {customer}", cache.get(KEY, now), db.price, when=f"{now}s ")
        show.beat(0.7)
    stale_served = show.wrong

    customer += 1
    who = f"Cust {customer}"
    show.clock = TTL
    cache.get(KEY, show.clock)
    show.op(who, "GET → miss (expired)")
    view.flight = (who, "looked in the cache: miss", "")
    show.say(f"At {TTL} seconds the entry expires.\nThe next customer gets a miss…", 1.8)
    price = db.price
    show.op(who, f"DB read → {rupees(price)}")
    cache.set(KEY, price, show.clock, ttl=TTL)
    show.op(who, f"SET {rupees(price)}, TTL {TTL} s", True)
    show.serve(who, price, db.price, hit=False)
    view.flight = None
    show.say(f"…reloads {rupees(price)} from the database, and the cache heals.", 1.8)
    for now in (TTL + 10, TTL + 20):
        customer += 1
        show.clock = now
        show.serve(f"Cust {customer}", cache.get(KEY, now), db.price, when=f"{now}s ")
        show.beat(0.6)

    show.judge(
        False,
        f"{stale_served} wrong prices before it expired",
        row=(f"+ TTL {TTL} s", "slow read + update", str(stale_served), f"stale for {TTL} s"),
    )
    show.say("TTL limits the damage.\nIt does [bold]not prevent[/] the stale write.", 2.6)
    return Recap(
        rows=[
            ("0 s", "same race", f"cache = {rupees(held)}", f"TTL {TTL} s"),
            (f"10 to {TTL - 10} s", f"{stale_served} customers", f"shown {rupees(held)}", f"[{BAD}]wrong[/]"),
            (f"{TTL} s", "entry expires", f"reloads {rupees(NEW_PRICE)}", ""),
            (f"{TTL + 10} to {TTL + 20} s", "2 customers", f"shown {rupees(NEW_PRICE)}", f"[{OK}]right[/]"),
        ],
        notes=[
            ("WHAT TTL DOES", "Every entry deletes itself after a set time, so a stale value "
                              "cannot live forever."),
            ("NOT ENOUGH", f"The stale write still happened. TTL only limits how long it is served: "
                           f"here {TTL} s and {stale_served} wrong prices. A shorter TTL means less "
                           "staleness but more database load."),
            ("REMEMBER", "TTL says \"eventually remove the stale value\". It is a safety net, "
                         "not a fix."),
        ],
    )


def act_lease(show: Show) -> Recap:
    db, cache = Database(), LeaseCache()
    view = StoreView(show, db, cache)
    show.scene(4, "LEASE TOKEN", view)

    show.say("Better fix: on a miss the cache hands out a [bold]lease token[/].\nOnly a valid token may fill the key.", 2.4)
    _, lease = cache.get_or_lease(KEY, show.clock)
    show.op(READER, f"GET → miss, lease #{lease}")
    view.flight = (READER, f"holding lease #{lease}", "")
    view.cache_note = f"lease #{lease} given to {READER}"
    show.say(f"User A gets a cache miss, and with it [bold]lease #{lease}[/]:\npermission to fill this key.", 2.0)
    held = db.price
    show.op(READER, f"DB read → {rupees(held)}")
    view.flight = (READER, f"holding lease #{lease} and {rupees(held)}", "")
    show.say(f"A reads {rupees(held)} from the database and stalls, exactly as before.", 1.7)

    db.price = NEW_PRICE
    show.op(WRITER, f"DB write {rupees(NEW_PRICE)}")
    view.flight = (READER, f"holding {rupees(held)} · now out of date", BAD)
    show.say(f"The admin changes the price to {rupees(NEW_PRICE)}…", 1.5)
    cache.delete(KEY)
    show.op(WRITER, f"DEL cancels lease #{lease}", True)
    view.cache_note = f"lease #{lease} cancelled"
    show.say(f"…and deletes the key. It is empty, but this time\nthe delete also [bold]cancels lease #{lease}[/].", 2.2)

    stored = cache.set_with_lease(KEY, held, lease, show.clock)
    show.op(READER, f"SET {rupees(held)} → {'stored' if stored else 'REFUSED'}", not stored)
    view.flight = (READER, "write refused: lease cancelled", OK)
    show.say(f"A wakes up and tries to save {rupees(held)} with lease #{lease}.\nThe cache [bold {OK}]refuses[/]: that lease is dead.", 2.4)

    who = "Cust 1"
    _, fresh = cache.get_or_lease(KEY, show.clock)
    show.op(who, f"GET → miss, lease #{fresh}")
    view.flight = (who, f"holding lease #{fresh}", "")
    view.cache_note = f"lease #{fresh} given to {who}"
    show.say("The cache is still empty, so the next customer\ngets a miss and a [bold]fresh lease[/].", 1.8)
    price = db.price
    show.op(who, f"DB read → {rupees(price)}")
    accepted = cache.set_with_lease(KEY, price, fresh, show.clock)
    show.op(who, f"SET {rupees(price)} ✓ lease #{fresh}", accepted)
    show.serve(who, price, db.price, hit=False)
    view.flight, view.cache_note = None, ""
    show.say(f"It reads {rupees(price)}. Its lease is still valid, so the cache accepts the write.", 1.8)

    show.narration = f"Everyone after that is served {rupees(NEW_PRICE)} from the cache."
    for n in range(2, CUSTOMERS + 1):
        value, _ = cache.get_or_lease(KEY, show.clock)
        show.serve(f"Cust {n}", value, db.price)
        show.beat(0.45)

    show.judge(
        True,
        f"old value refused, {show.wrong} wrong prices",
        row=("+ lease token", "slow read + update", str(show.wrong), "holds"),
    )
    show.say(f"The old value never got back in.\n[bold {OK}]No customer saw a wrong price.[/]", 2.6)
    return Recap(
        rows=[
            (READER, f"miss, lease #{lease}", f"reads DB: {rupees(held)}", "then stalls"),
            (WRITER, f"DB → {rupees(NEW_PRICE)}", "deletes key", f"lease #{lease} dead"),
            (READER, f"SET, lease #{lease}", f"[{OK}]refused[/]", "cache stays empty"),
            (f"Cust 1-{CUSTOMERS}", f"new lease #{fresh}", f"shown {rupees(NEW_PRICE)}", f"[{OK}]right[/]"),
        ],
        notes=[
            ("HOW IT WORKS", "On a miss the cache gives the reader a lease token. A write is "
                             "accepted only with the key's current token, and deleting the key cancels it."),
            ("WHY IT HOLDS", "The admin's delete now leaves a mark even on an empty key: the "
                             "lease it kills belonged to the slow reader, whose old value is now refused."),
            ("WHERE FROM", "Facebook described leases for memcached in 2013. TTL removes a "
                           "stale value eventually; a lease stops it being written."),
        ],
    )


def finale(show: Show) -> None:
    show.act = FINALE
    show.beat(3.6)


ACTS = [act_rule, act_race, act_ttl, act_lease]


def main() -> int:
    parser = argparse.ArgumentParser(description="Live terminal demo: keeping a cache and a database in sync.")
    parser.add_argument("acts", nargs="*", type=int, choices=range(1, len(ACTS) + 1), metavar="ACT",
                        help="acts to play, 1-4 (default: the full show)")
    parser.add_argument("--speed", type=float, default=1.0, help="playback speed, e.g. 0.5 or 2 (default: 1)")
    parser.add_argument("--auto", action="store_true", help="don't stop for Enter after each act")
    args = parser.parse_args()
    if args.speed <= 0:
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
    show = Show(console, speed=args.speed, total=rehearsal.elapsed, stops=stops)
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
