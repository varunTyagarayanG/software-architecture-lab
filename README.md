# Software Architecture Lab

Small, self-contained projects that each take one backend or system-design idea and make it visible: a plain-language explanation, plus a simulation you run in a terminal and watch.

## Why this repo exists

These ideas come up all the time in backend work and in system-design interviews. They are easy to read about and hard to remember.

"A fixed window can let through twice the limit at the boundary" is one sentence in a textbook. Watching 20 logins get past a 10-per-minute limiter in two seconds is something you don't forget. Every project here is built to give you the second kind of understanding.

## Projects

| # | Project | Idea it covers | Run it |
|---|---|---|---|
| 1 | [Rate limiter Vs Buckets](Rate%20limiter%20Vs%20Buckets/) | Protecting a login API from password guessing: fixed window counter, its boundary problem, token bucket, shared Redis state | `python3 simulate.py` |

New projects get a row in this table and a short section below when they are added.

### 1. Rate limiter Vs Buckets

**What it is.** A live terminal demo of a login API under a password-guessing attack, defended by two different rate limiters.

**Why we need it.** Without a limit, an attacker can try passwords for someone else's account as fast as the server will answer. A rate limiter is the guard that stops this, but the simplest design has a hole, and a limiter that works on one server silently stops working on ten. You need to know both failure modes before you build or explain one.

**How it works.** The demo attacks real limiter code in four acts:

1. A **fixed window counter** correctly blocks the 11th login in a minute.
2. The same counter is tricked at the minute boundary: 20 logins get through in 2 seconds.
3. A **token bucket** faces the same attack and lets only 10 through.
4. Ten servers with their own counters let 30 through; one shared counter in **Redis** lets 10 through.

After each act it stops on a summary and waits for Enter.

**Read more.** The [project README](Rate%20limiter%20Vs%20Buckets/README.md) explains every idea in detail and walks through the simulation with screenshots.

## How each project is laid out

Every project lives in its own folder and follows the same shape, so you always know where to look:

| File | What it holds |
|---|---|
| `README.md` | The full explanation: the problem, each approach, and a screenshot walkthrough of the simulation |
| `notes.md` | Short revision notes, including a ready interview answer |
| `simulate.py` | The simulation. One file, run it directly |
| `images/` | Screenshots used by the README |

## Running a project

You need Python 3 (tested on 3.12) and the [`rich`](https://github.com/Textualize/rich) library, which draws the terminal screens:

```bash
pip install rich
cd "Rate limiter Vs Buckets"
python3 simulate.py
```

Use a terminal window of at least 80 columns by 24 rows. A project's own README lists its options.
