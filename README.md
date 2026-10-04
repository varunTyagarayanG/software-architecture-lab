# Software Architecture Lab

Small, self-contained projects that each take one backend or system-design idea and make it visible: a plain-language explanation, plus a simulation you run in a terminal and watch.

## Why this repo exists

These ideas come up all the time in backend work and in system-design interviews. They are easy to read about and hard to remember.

"A fixed window can let through twice the limit at the boundary" is one sentence in a textbook. Watching 20 logins get past a 10-per-minute limiter in two seconds is something you don't forget. Every project here is built to give you the second kind of understanding.

## Projects

| # | Project | Idea it covers | Run it |
|---|---|---|---|
| 1 | [fixed-window-vs-token-bucket](fixed-window-vs-token-bucket/) | Protecting a login API from password guessing: fixed window counter, its boundary problem, token bucket, shared Redis state | `python3 simulate.py` |
| 2 | [cache-database-synchronization](cache-database-synchronization/) | Keeping a cache in step with the database when data changes: update-then-delete, the stale-write race, TTL, lease tokens | `python3 simulate.py` |
| 3 | [free-trial-abuse-prevention](free-trial-abuse-prevention/) | Stopping people taking a free trial twice: IP limits, device fingerprinting, risk scores, challenges | `python3 simulate.py` |

New projects get a row in this table and a short section below when they are added.

### 1. fixed-window-vs-token-bucket

**What it is.** A live terminal demo of a login API under a password-guessing attack, defended by two different rate limiters.

**Why we need it.** Without a limit, an attacker can try passwords for someone else's account as fast as the server will answer. A rate limiter is the guard that stops this, but the simplest design has a hole, and a limiter that works on one server silently stops working on ten. You need to know both failure modes before you build or explain one.

**How it works.** The demo attacks real limiter code in four acts:

1. A **fixed window counter** correctly blocks the 11th login in a minute.
2. The same counter is tricked at the minute boundary: 20 logins get through in 2 seconds.
3. A **token bucket** faces the same attack and lets only 10 through.
4. Ten servers with their own counters let 30 through; one shared counter in **Redis** lets 10 through.

After each act it stops on a summary and waits for Enter.

**Read more.** The [project README](fixed-window-vs-token-bucket/README.md) explains every idea in detail and walks through the simulation with screenshots.

### 2. cache-database-synchronization

**What it is.** A live terminal demo of an online store that keeps prices in a database and a fast copy in a cache, while one price changes from ₹100 to ₹200.

**Why we need it.** A cache makes reads fast, but it is a second copy of the truth. If it is not kept in step with the database, customers are shown a price that no longer exists. The usual rule for keeping them in step has a race condition that is easy to miss and hard to notice in production.

**How it works.** The demo runs the same price change four times against real cache code:

1. **The rule**, with no overlap: update the database, then delete the cache key. Every customer sees the right price.
2. **The race**: a slow reader fetches the old price, the update and delete happen, and the reader then writes the old price back. The cache is wrong for good.
3. **TTL**: the same race, but the stale entry expires after 60 seconds. Five customers still see the wrong price.
4. **Lease token**: the same race, but the delete cancels the reader's lease and the cache refuses its stale write. Nobody sees a wrong price.

After each act it stops on a summary and waits for Enter.

**Read more.** The [project README](cache-database-synchronization/README.md) explains every idea in detail and walks through the simulation with screenshots.

### 3. free-trial-abuse-prevention

**What it is.** A live terminal demo of ten signups arriving at a service that gives one free trial per person. Five are honest people; one person comes back five times.

**Why we need it.** Every defence here is a guess about identity, and a guess can be wrong twice over: it can turn away a customer who never had a trial, or hand another free month to someone on their fifth account. Watching both error counts at once is the only way to see that a stricter rule is not automatically a better one.

**How it works.** The same ten signups are run past four policies:

1. **One trial per IP address**: blocks an office and a household, and misses a mobile hotspot. Three honest people blocked, two repeats let in.
2. **Device fingerprint**: the office is fixed, but opening a second browser looks like a new device. One blocked, three let in.
3. **Risk score**: six weak signals, weighted and added up. Every repeat stopped, but a man using his wife's laptop is denied too.
4. **Risk score + challenge**: the uncertain middle is asked to verify a payment method. Nobody honest blocked, no repeat let in.

After each act it stops on a summary and waits for Enter.

**Read more.** The [project README](free-trial-abuse-prevention/README.md) explains every idea in detail and walks through the simulation with screenshots.

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
cd fixed-window-vs-token-bucket      # or any other project folder
python3 simulate.py
```

Use a terminal window of at least 80 columns by 24 rows.

Every simulation opens the same way, by asking how you want to watch it:

```text
 1  Step by step  ENTER forward, ← back
 2  Normal        plays itself
 3  Slow          half speed
 4  Fast          double speed
```

Whichever you pick, the show stops on a summary after each act until you press Enter. Stepping also goes backwards, with ←, Backspace or Shift+Enter. A project's own README lists the rest of its options.
