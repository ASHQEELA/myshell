"""A small scheduler: `cron` for repeating tasks and `at` for one-time tasks.

Tasks are saved in ~/.myshell_sched.json. While an interactive myshell is open, a
background thread checks them every 20 seconds. Each due task runs in its own
`myshell -c` process and writes to ~/.myshell_cron.log. Nothing runs while no myshell
is open. A one-time `at` task that came due meanwhile runs when the next shell starts."""
import contextlib
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Dict, Iterator, List, Optional, Set, TextIO, Tuple

from .errors import ShellError
from .paths import home_file
from .state import ShellState

CHECK_SECONDS = 20

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
MACROS = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *",
          "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
          "@hourly": "0 * * * *"}


# ---------------------------------------------------------------- cron expressions

class CronError(ValueError):
    pass


def _field(text: str, low: int, high: int, names: Optional[List[str]], label: str) -> Set[int]:
    """One cron field: numbers, names, *, ranges a-b, lists a,b and steps /n."""
    values = set()  # type: Set[int]

    def number(tok: str) -> int:
        if names and tok.lower()[:3] in names and not tok.isdigit():
            return names.index(tok.lower()[:3]) + (low if label != "day of week" else 0)
        if not tok.isdigit():
            raise CronError(f"{label}: '{tok}' is not a number")
        return int(tok)

    for part in text.split(","):
        if not part:
            raise CronError(f"{label}: empty item in '{text}'")
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"{label}: bad step '{step_text}'")
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = number(a), number(b)
        else:
            start = number(part)
            end = high if step > 1 else start
        if start < low or end > high or start > end:
            raise CronError(f"{label}: {part or text} is outside {low}-{high}")
        values.update(range(start, end + 1, step))
    if label == "day of week" and 7 in values:
        values.discard(7)
        values.add(0)
    return values


class CronSpec:
    def __init__(self, expr: str):
        self.expr = expr
        text = MACROS.get(expr.strip().lower(), expr)
        parts = text.split()
        if len(parts) != 5:
            raise CronError("a schedule needs 5 fields: minute hour day month weekday")
        self.minutes = _field(parts[0], 0, 59, None, "minute")
        self.hours = _field(parts[1], 0, 23, None, "hour")
        self.days = _field(parts[2], 1, 31, None, "day of month")
        self.months = _field(parts[3], 1, 12, MONTHS, "month")
        self.weekdays = _field(parts[4], 0, 7, DAYS, "day of week")
        self.any_day = parts[2].startswith("*")
        self.any_weekday = parts[4].startswith("*")

    def day_matches(self, d: dt.date) -> bool:
        if d.month not in self.months:
            return False
        dom_ok = d.day in self.days
        dow_ok = (d.isoweekday() % 7) in self.weekdays
        if self.any_day or self.any_weekday:
            return dom_ok and dow_ok
        return dom_ok or dow_ok  # cron runs when either field matches if both are set

    def matches(self, when: dt.datetime) -> bool:
        return (when.minute in self.minutes and when.hour in self.hours
                and self.day_matches(when.date()))

    def next_after(self, when: dt.datetime) -> Optional[dt.datetime]:
        """The first matching minute strictly after when. None if none in four years."""
        start = when.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        day = start.date()
        for _ in range(366 * 4 + 1):
            if self.day_matches(day):
                for h in sorted(self.hours):
                    for m in sorted(self.minutes):
                        cand = dt.datetime.combine(day, dt.time(h, m))
                        if cand >= start:
                            return cand
            day += dt.timedelta(days=1)
        return None


# ---------------------------------------------------------------- at times

UNITS = {"m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
         "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
         "d": 86400, "day": 86400, "days": 86400, "w": 604800, "week": 604800, "weeks": 604800,
         "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1}


def _clock(token: str, meridiem: Optional[str]) -> Optional[Tuple[int, int]]:
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", token.lower())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    mer = m.group(3) or (meridiem.lower() if meridiem else None)
    if mer is None and m.group(2) is None:
        return None  # a bare number is not a time
    if mer:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if mer == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def parse_at(args: List[str], now: dt.datetime) -> Tuple[dt.datetime, List[str]]:
    """Split `TIME COMMAND...` into (when, command words). Raises ShellError."""
    if not args:
        raise ShellError("at: usage: at TIME COMMAND  (for example: at 17:30 echo hi)")
    first = args[0].lower()
    # relative: "now + 10 minutes", "+10m", "+ 10 min"
    rel = None
    used = 0
    if first == "now" and len(args) >= 2:
        tail = args[1:]
        if tail[0] == "+" and len(tail) >= 3:
            rel, used = (tail[1], tail[2]), 4
        elif tail[0].startswith("+") and len(tail[0]) > 1:
            m = re.fullmatch(r"\+(\d+)([a-z]*)", tail[0].lower())
            if m and m.group(2):
                rel, used = (m.group(1), m.group(2)), 2
            elif m and len(tail) >= 2:
                rel, used = (m.group(1), tail[1]), 3
    elif first.startswith("+"):
        m = re.fullmatch(r"\+(\d+)([a-z]*)", first)
        if m and m.group(2):
            rel, used = (m.group(1), m.group(2)), 1
        elif m and len(args) >= 2:
            rel, used = (m.group(1), args[1]), 2
        elif first == "+" and len(args) >= 3:
            rel, used = (args[1], args[2]), 3
    if rel is not None:
        number, unit = rel
        if not number.isdigit() or unit.lower() not in UNITS:
            raise ShellError("at: after + give a number and a unit, such as +10m or now + 2 hours")
        return now + dt.timedelta(seconds=int(number) * UNITS[unit.lower()]), args[used:]
    # absolute date and time: 2026-12-01 09:30
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", first) and len(args) >= 2:
        clock = _clock(args[1], args[2] if len(args) > 2 else None)
        if clock:
            used = 3 if len(args) > 2 and args[2].lower() in ("am", "pm") else 2
            try:
                day = dt.date.fromisoformat(first)
            except ValueError:
                raise ShellError(f"at: {first} is not a real date")
            return dt.datetime.combine(day, dt.time(*clock)), args[used:]
    nxt = args[1].lower() if len(args) > 1 else None
    clock = _clock(first, nxt if nxt in ("am", "pm") else None)
    if clock:
        used = 2 if nxt in ("am", "pm") and re.fullmatch(r"\d{1,2}(:\d{2})?", first) else 1
        when = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        if when <= now:
            when += dt.timedelta(days=1)  # that time already passed today
        return when, args[used:]
    raise ShellError(f"at: cannot read the time '{args[0]}'. "
                     "Use 17:30, 5pm, +10m, now + 2 hours or 2026-12-01 09:30.")


# ---------------------------------------------------------------- storage

def data_file() -> str:
    return home_file(".myshell_sched.json")


def log_file() -> str:
    return home_file(".myshell_cron.log")


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """A lock file shared by every myshell, so two windows do not run one task twice."""
    path = data_file() + ".lock"
    fd = None
    for _ in range(40):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > 10:
                    os.remove(path)  # left behind by a crashed shell
                    continue
            except OSError:
                pass
            time.sleep(0.05)
        except OSError:
            break
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            try:
                os.remove(path)
            except OSError:
                pass


def load() -> Dict:
    try:
        with open(data_file()) as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("cron", [])
            data.setdefault("at", [])
            data.setdefault("next_id", 1)
            return data
    except (OSError, ValueError):
        pass
    return {"cron": [], "at": [], "next_id": 1}


def save(data: Dict) -> None:
    path = data_file()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- running tasks

def _spawn(cmd: str, label: str) -> "subprocess.Popen":
    """Run cmd in a fresh myshell. Output goes to the log file."""
    with open(log_file(), "a", encoding="utf-8", errors="replace") as log:
        log.write(f"--- {dt.datetime.now():%Y-%m-%d %H:%M:%S} {label}: {cmd}\n")
    out = open(log_file(), "a", encoding="utf-8", errors="replace")
    try:
        kwargs = {"start_new_session": True} if os.name != "nt" else {}
        return subprocess.Popen([sys.executable, "-m", "myshell", "-c", cmd],
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                cwd=os.path.expanduser("~") if not os.environ.get("HOME")
                                else os.environ["HOME"], **kwargs)
    finally:
        out.close()


class Scheduler:
    def __init__(self) -> None:
        self.notices = []  # type: List[str]
        self.running = []  # type: List[subprocess.Popen]
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._thread = None  # type: Optional[threading.Thread]

    def tick(self, now: Optional[dt.datetime] = None) -> List[str]:
        """Start every task that is due. Returns a line for each one started."""
        now = now or dt.datetime.now()
        stamp = int(now.replace(second=0, microsecond=0).timestamp() // 60)
        due = []  # type: List[Tuple[str, str]]
        with _locked():
            data = load()
            if not data["cron"] and not data["at"]:
                self._reap()
                return []
            changed = False
            for task in data["cron"]:
                try:
                    spec = CronSpec(task["expr"])
                except CronError:
                    continue
                if task.get("last", 0) < stamp and spec.matches(now):
                    task["last"] = stamp
                    due.append((f"cron #{task['id']}", task["cmd"]))
                    changed = True
            keep = []
            for job in data["at"]:
                if job["when"] <= now.timestamp():
                    due.append((f"at #{job['id']}", job["cmd"]))
                    changed = True
                else:
                    keep.append(job)
            data["at"] = keep
            if changed:
                save(data)
        lines = []
        for label, cmd in due:
            try:
                proc = _spawn(cmd, label)
                self.running.append(proc)
                lines.append(f"[{label}] started: {cmd}")
            except OSError as e:
                lines.append(f"[{label}] could not start: {e}")
        with self._guard:
            self.notices.extend(lines)
        self._reap()
        return lines

    def _reap(self) -> None:
        self.running = [p for p in self.running if p.poll() is None]

    def take_notices(self) -> List[str]:
        with self._guard:
            out, self.notices = self.notices, []
        return out

    def _loop(self) -> None:
        while not self._stop.wait(CHECK_SECONDS):
            try:
                self.tick()
            except Exception:  # the scheduler must never hurt the shell
                pass

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            self.tick()  # runs one-time tasks that came due while no shell was open
        except Exception:
            pass
        self._thread = threading.Thread(target=self._loop, name="myshell-sched", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread = None


SCHEDULER = Scheduler()


def start_background() -> bool:
    if os.environ.get("MYSHELL_NOCRON") == "1":
        return False
    SCHEDULER.start()
    return True


# ---------------------------------------------------------------- builtins

def _show_time(when: Optional[dt.datetime]) -> str:
    return when.strftime("%Y-%m-%d %H:%M") if when else "never"


def _split_expr(args: List[str]) -> Tuple[str, List[str]]:
    if not args:
        raise ShellError("cron add: usage: cron add 'MIN HOUR DAY MONTH WEEKDAY' COMMAND")
    if args[0].lower() in MACROS:
        return args[0], args[1:]
    if " " in args[0]:
        return args[0], args[1:]
    if len(args) >= 5:
        return " ".join(args[:5]), args[5:]
    raise ShellError("cron add: a schedule has 5 fields. Quote it: cron add '*/5 * * * *' CMD")


CRON_HELP = """cron: repeating tasks, run while myshell is open
  cron                      list tasks
  cron add 'EXPR' COMMAND   add a task, for example: cron add '*/5 * * * *' echo hi
  cron rm ID                remove a task
  cron run ID               run a task now in this shell
  cron next [ID]            show the next run times
  cron check 'EXPR'         test a schedule without saving it
  cron log [N | --clear]    show the last N log lines (20 by default), or clear the log
  cron status               show what the scheduler is doing
EXPR is: minute hour day month weekday. Use * for every, */5 for every 5th, 1-5 for a range,
1,15 for a list. Or use @hourly, @daily, @weekly, @monthly. Quote the schedule so the
shell does not expand the stars."""


def builtin_cron(state: ShellState, args: List[str], stdout: TextIO, stderr: TextIO) -> int:
    sub = args[0] if args else "list"
    rest = args[1:]
    try:
        if sub in ("-h", "--help", "help"):
            print(CRON_HELP, file=stdout)
            return 0
        if sub in ("list", "ls", "-l"):
            return _cron_list(stdout)
        if sub == "add":
            return _cron_add(rest, stdout)
        if sub in ("rm", "remove", "del", "delete"):
            return _remove("cron", rest, stdout, stderr)
        if sub == "run":
            return _cron_run(state, rest, stderr)
        if sub == "next":
            return _cron_next(rest, stdout, stderr)
        if sub == "check":
            return _cron_check(rest, stdout, stderr)
        if sub == "log":
            return _cron_log(rest, stdout, stderr)
        if sub == "status":
            return _cron_status(stdout)
    except ShellError as e:
        print(e, file=stderr)
        return 2
    print(f"cron: unknown command '{sub}'. Try: cron --help", file=stderr)
    return 2


def _cron_list(stdout: TextIO) -> int:
    data = load()
    if not data["cron"]:
        print("No cron tasks. Add one: cron add '*/5 * * * *' echo hi", file=stdout)
        return 0
    now = dt.datetime.now()
    for t in data["cron"]:
        try:
            nxt = _show_time(CronSpec(t["expr"]).next_after(now))
        except CronError:
            nxt = "bad schedule"
        print(f"{t['id']:>3}  {t['expr']:<15} next {nxt}  {t['cmd']}", file=stdout)
    return 0


def _cron_add(args: List[str], stdout: TextIO) -> int:
    expr, cmd_words = _split_expr(args)
    try:
        spec = CronSpec(expr)
    except CronError as e:
        raise ShellError(f"cron add: {e}")
    if not cmd_words:
        raise ShellError("cron add: give a command after the schedule")
    cmd = " ".join(cmd_words)
    with _locked():
        data = load()
        tid = data["next_id"]
        data["next_id"] = tid + 1
        data["cron"].append({"id": tid, "expr": expr, "cmd": cmd, "last": 0})
        save(data)
    print(f"cron #{tid} added. Next run: {_show_time(spec.next_after(dt.datetime.now()))}",
          file=stdout)
    return 0


def _remove(kind: str, args: List[str], stdout: TextIO, stderr: TextIO) -> int:
    if not args or not all(a.lstrip("#").isdigit() for a in args):
        print(f"{kind}: give the task number, for example: {kind} rm 1", file=stderr)
        return 2
    status = 0
    with _locked():
        data = load()
        key = "cron" if kind == "cron" else "at"
        for a in args:
            tid = int(a.lstrip("#"))
            before = len(data[key])
            data[key] = [t for t in data[key] if t["id"] != tid]
            if len(data[key]) == before:
                print(f"{kind}: no task number {tid}", file=stderr)
                status = 1
            else:
                print(f"{kind} #{tid} removed", file=stdout)
        save(data)
    return status


def _find_task(args: List[str], name: str) -> Dict:
    if len(args) != 1 or not args[0].isdigit():
        raise ShellError(f"cron {name}: give the task number")
    for t in load()["cron"]:
        if t["id"] == int(args[0]):
            return t
    raise ShellError(f"cron {name}: no task number {args[0]}")


def _cron_run(state: ShellState, args: List[str], stderr: TextIO) -> int:
    task = _find_task(args, "run")
    return state.execute(task["cmd"])


def _cron_next(args: List[str], stdout: TextIO, stderr: TextIO) -> int:
    tasks = [_find_task(args, "next")] if args else load()["cron"]
    if not tasks:
        print("No cron tasks.", file=stdout)
    for t in tasks:
        spec = CronSpec(t["expr"])
        when, shown = dt.datetime.now(), []
        for _ in range(3):
            when = spec.next_after(when)
            if when is None:
                break
            shown.append(_show_time(when))
        print(f"#{t['id']} {t['cmd']}: " + ", ".join(shown or ["never"]), file=stdout)
    return 0


def _cron_check(args: List[str], stdout: TextIO, stderr: TextIO) -> int:
    expr = " ".join(args)
    try:
        spec = CronSpec(expr)
    except CronError as e:
        print(f"cron check: {e}", file=stderr)
        return 1
    when, shown = dt.datetime.now(), []
    for _ in range(3):
        when = spec.next_after(when)
        if when is None:
            break
        shown.append(_show_time(when))
    print("OK. Next runs: " + ", ".join(shown or ["never"]), file=stdout)
    return 0


def _cron_log(args: List[str], stdout: TextIO, stderr: TextIO) -> int:
    if args and args[0] == "--clear":
        try:
            os.remove(log_file())
        except OSError:
            pass
        print("cron log cleared", file=stdout)
        return 0
    count = 20
    if args:
        if not args[0].isdigit():
            print("cron log: usage: cron log [N | --clear]", file=stderr)
            return 2
        count = int(args[0])
    try:
        with open(log_file(), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        print("The log is empty.", file=stdout)
        return 0
    for line in lines[-count:]:
        print(line, file=stdout)
    return 0


def _cron_status(stdout: TextIO) -> int:
    data = load()
    on = SCHEDULER._thread is not None
    print(f"scheduler: {'running in this shell' if on else 'not running in this shell'}",
          file=stdout)
    print(f"cron tasks: {len(data['cron'])}, waiting at tasks: {len(data['at'])}", file=stdout)
    print(f"log: {log_file()}", file=stdout)
    return 0


def builtin_at(state: ShellState, args: List[str], stdout: TextIO, stderr: TextIO) -> int:
    """at TIME COMMAND | at -l | at -r ID"""
    if args and args[0] in ("-h", "--help"):
        print("at: run a command once, while myshell is open\n"
              "  at 17:30 echo hi          at a clock time (tomorrow if it has passed)\n"
              "  at 5pm backup.sh          12 hour time\n"
              "  at +10m echo hi           in 10 minutes. Units: m, h, d, w\n"
              "  at now + 2 hours echo hi  the long form\n"
              "  at 2026-12-01 09:30 cmd   a date and time\n"
              "  at -l                     list waiting tasks\n"
              "  at -r ID                  remove a task", file=stdout)
        return 0
    if args and args[0] in ("-l", "list"):
        data = load()
        if not data["at"]:
            print("No waiting at tasks.", file=stdout)
        for j in sorted(data["at"], key=lambda j: j["when"]):
            print(f"{j['id']:>3}  {dt.datetime.fromtimestamp(j['when']):%Y-%m-%d %H:%M}  "
                  f"{j['cmd']}", file=stdout)
        return 0
    if args and args[0] in ("-r", "rm"):
        return _remove("at", args[1:], stdout, stderr)
    try:
        when, words = parse_at(args, dt.datetime.now())
    except ShellError as e:
        print(e, file=stderr)
        return 2
    if not words:
        print("at: give a command after the time", file=stderr)
        return 2
    with _locked():
        data = load()
        tid = data["next_id"]
        data["next_id"] = tid + 1
        data["at"].append({"id": tid, "when": when.timestamp(), "cmd": " ".join(words)})
        save(data)
    print(f"at #{tid} will run at {when:%Y-%m-%d %H:%M}", file=stdout)
    return 0


BUILTINS = {"cron": builtin_cron, "at": builtin_at}
