"""Process tools: ps, top and kill, working the same on Linux, macOS and Windows.

list_processes() reads /proc on Linux, `ps` on macOS and BSD, and `tasklist` on Windows.
The parsers are separate functions so tests can feed them sample output."""
import csv
import io
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, TextIO

from . import compat
from .state import ShellState

_sleep = time.sleep  # tests replace this so top does not wait


@dataclass
class Proc:
    pid: int
    ppid: Optional[int]      # None when the platform does not report it
    name: str
    rss_kb: int              # resident memory in KiB
    state: str = "?"
    ticks: Optional[int] = None   # CPU time used, in clock ticks. None when unknown.


# ---------------------------------------------------------------- reading the system

def _clock_ticks() -> int:
    try:
        return os.sysconf("SC_CLK_TCK")
    except (AttributeError, ValueError, OSError):
        return 100


def _read_proc() -> List[Proc]:
    """Linux: one entry per /proc/PID folder."""
    page_kb = 4
    try:
        page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
    except (AttributeError, ValueError, OSError):
        pass
    found = []  # type: List[Proc]
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                text = f.read()
            left, right = text.find("("), text.rfind(")")
            name = text[left + 1:right]
            rest = text[right + 2:].split()
            rss = int(rest[21]) * page_kb  # field 24 of stat, counted in pages
            found.append(Proc(int(entry), int(rest[1]), name, rss, rest[0],
                              int(rest[11]) + int(rest[12])))
        except (OSError, ValueError, IndexError):
            continue  # the process ended while we looked
    return found


def parse_ps_output(text: str) -> List[Proc]:
    """Output of: ps -axo pid=,ppid=,rss=,stat=,comm="""
    found = []
    for line in text.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        try:
            found.append(Proc(int(parts[0]), int(parts[1]), os.path.basename(parts[4]),
                              int(parts[2]), parts[3]))
        except ValueError:
            continue
    return found


def parse_tasklist(text: str) -> List[Proc]:
    """Output of: tasklist /FO CSV /NH. Columns: name, pid, session, session number, memory."""
    found = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 5 or not row[1].strip().isdigit():
            continue
        digits = "".join(ch for ch in row[4] if ch.isdigit())
        found.append(Proc(int(row[1]), None, row[0], int(digits) if digits else 0, "R"))
    return found


def list_processes() -> List[Proc]:
    if os.path.isdir("/proc") and os.path.exists("/proc/self/stat"):
        return _read_proc()
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                             text=True, errors="replace").stdout
        return parse_tasklist(out)
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,stat=,comm="], capture_output=True,
                         text=True, errors="replace").stdout
    return parse_ps_output(out)


def descendants(procs: List[Proc], root: int) -> List[Proc]:
    """The process with pid root and everything it started."""
    by_parent = {}  # type: Dict[int, List[Proc]]
    for p in procs:
        by_parent.setdefault(p.ppid if p.ppid is not None else -1, []).append(p)
    out = [p for p in procs if p.pid == root]
    queue = [root]
    while queue:
        for child in by_parent.get(queue.pop(0), []):
            out.append(child)
            queue.append(child.pid)
    return out


# ---------------------------------------------------------------- ps

PS_ALL_FLAGS = {"-e", "-A", "-a", "-x", "-ax", "-aux", "-ef", "-eF", "aux", "ax", "-axu"}


def _format_mem(kb: int) -> str:
    if kb >= 1024 * 1024:
        return f"{kb / 1024 / 1024:.1f}G"
    if kb >= 1024:
        return f"{kb / 1024:.1f}M"
    return f"{kb}K"


def builtin_ps(state: ShellState, args: List[str], stdout: TextIO, stderr: TextIO) -> int:
    """ps [-e]: list your shell's processes, or every process with -e."""
    show_all = False
    for arg in args:
        if arg in PS_ALL_FLAGS:
            show_all = True
        else:
            print(f"ps: {arg}: unknown option. Usage: ps [-e]", file=stderr)
            return 2
    procs = list_processes()
    if not show_all and any(p.ppid is not None for p in procs):
        mine = descendants(procs, os.getpid())
        if mine:
            procs = mine
    procs.sort(key=lambda p: p.pid)
    print(f"{'PID':>7} {'PPID':>7} {'STAT':<5} {'MEM':>7}  NAME", file=stdout)
    for p in procs:
        ppid = "-" if p.ppid is None else str(p.ppid)
        print(f"{p.pid:>7} {ppid:>7} {p.state[:5]:<5} {_format_mem(p.rss_kb):>7}  {p.name}",
              file=stdout)
    return 0


# ---------------------------------------------------------------- top

def _total_memory_kb() -> Optional[int]:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def cpu_percent(before: List[Proc], after: List[Proc], seconds: float) -> Dict[int, float]:
    """CPU use per pid between two samples, in percent of one core."""
    old = {p.pid: p.ticks for p in before if p.ticks is not None}
    out = {}
    if seconds <= 0:
        return out
    hz = _clock_ticks()
    for p in after:
        if p.ticks is not None and p.pid in old:
            out[p.pid] = max(0.0, (p.ticks - old[p.pid]) / hz / seconds * 100)
    return out


def top_frame(procs: List[Proc], cpu: Dict[int, float], sort: str, rows: int,
              stamp: str) -> List[str]:
    total = _total_memory_kb()
    head = f"myshell top - {stamp} - {len(procs)} processes"
    if total:
        used = sum(p.rss_kb for p in procs)
        head += f" - memory used by processes {_format_mem(used)} of {_format_mem(total)}"
    lines = [head]
    has_cpu = bool(cpu)
    key = {"cpu": lambda p: -cpu.get(p.pid, 0.0), "mem": lambda p: -p.rss_kb,
           "pid": lambda p: p.pid}[sort if (sort != "cpu" or has_cpu) else "mem"]
    cols = f"{'PID':>7} {'MEM':>7} " + (f"{'CPU%':>6} " if has_cpu else "") + "NAME"
    lines.append(cols)
    for p in sorted(procs, key=key)[:rows]:
        row = f"{p.pid:>7} {_format_mem(p.rss_kb):>7} "
        if has_cpu:
            row += f"{cpu.get(p.pid, 0.0):>6.1f} "
        lines.append(row + p.name)
    return lines


def builtin_top(state: ShellState, args: List[str], stdout: TextIO, stderr: TextIO) -> int:
    """top [-n COUNT] [-d SECONDS] [-s cpu|mem|pid] [-l ROWS]: a live process list.
    Without -n it refreshes until Ctrl+C on a terminal, and prints one frame otherwise."""
    count, delay, sort, rows = None, 1.0, "cpu", 15
    i = 0
    try:
        while i < len(args):
            flag = args[i]
            if flag in ("-n", "-d", "-s", "-l") and i + 1 < len(args):
                val = args[i + 1]
                if flag == "-n":
                    count = int(val)
                elif flag == "-d":
                    delay = float(val)
                elif flag == "-s":
                    if val not in ("cpu", "mem", "pid"):
                        raise ValueError(val)
                    sort = val
                else:
                    rows = int(val)
                i += 2
            elif flag == "-b":
                i += 1
            else:
                raise ValueError(flag)
        if delay < 0 or rows < 1 or (count is not None and count < 1):
            raise ValueError("range")
    except ValueError:
        print("top: usage: top [-n COUNT] [-d SECONDS] [-s cpu|mem|pid] [-l ROWS]", file=stderr)
        return 2
    tty = hasattr(stdout, "isatty") and stdout.isatty()
    if count is None:
        count = 10 ** 9 if tty else 1
    if tty and os.name == "nt":
        os.system("")  # turns on ANSI codes in the Windows console
    shown = 0
    try:
        before = list_processes()
        while shown < count:
            _sleep(delay)
            after = list_processes()
            cpu = cpu_percent(before, after, delay)
            lines = top_frame(after, cpu, sort, rows, time.strftime("%H:%M:%S"))
            if tty and shown:
                stdout.write("\x1b[H\x1b[J")
            print("\n".join(lines), file=stdout)
            stdout.flush()
            before = after
            shown += 1
            if shown < count and not tty:
                print(file=stdout)
    except KeyboardInterrupt:
        print(file=stdout)
    return 0


# ---------------------------------------------------------------- kill

def signal_table() -> Dict[str, int]:
    """Signal name (without SIG) to number, for the signals this OS has."""
    table = {}
    for name in dir(signal):
        if name.startswith("SIG") and not name.startswith("SIG_") and name.isupper():
            value = getattr(signal, name)
            if isinstance(value, int) or hasattr(value, "value"):
                table[name[3:]] = int(value)
    return table


def parse_signal(text: str) -> Optional[int]:
    table = signal_table()
    if text.isdigit():
        return int(text)
    name = text.upper()
    if name.startswith("SIG"):
        name = name[3:]
    return table.get(name)


def _list_signals(args: List[str], stdout: TextIO, stderr: TextIO) -> int:
    table = signal_table()
    if args:
        for a in args:
            if a.isdigit():
                names = [n for n, v in table.items() if v == int(a)]
                if not names:
                    print(f"kill: {a}: invalid signal specification", file=stderr)
                    return 1
                print(names[0], file=stdout)
            else:
                num = parse_signal(a)
                if num is None:
                    print(f"kill: {a}: invalid signal specification", file=stderr)
                    return 1
                print(num, file=stdout)
        return 0
    items = sorted(table.items(), key=lambda kv: (kv[1], kv[0]))
    seen = set()
    cells = []
    for name, num in items:
        if num in seen:
            continue
        seen.add(num)
        cells.append(f"{num:>2}) {name}")
    for k in range(0, len(cells), 5):
        print("  ".join(f"{c:<12}" for c in cells[k:k + 5]).rstrip(), file=stdout)
    return 0


def _alive(pid: int) -> bool:
    return any(p.pid == pid for p in list_processes())


def _send(pid: int, sig: int) -> None:
    if sig == 0:
        if os.name == "nt":
            if not _alive(pid):
                raise ProcessLookupError
            return
        os.kill(pid, 0)
        return
    os.kill(pid, sig)


def builtin_kill(state: ShellState, args: List[str], stdout: TextIO, stderr: TextIO) -> int:
    """kill [-s SIGNAL | -SIGNAL] PID|%JOB ...   kill -l [SIGNAL]"""
    usage = "kill: usage: kill [-s SIGNAL | -SIGNAL] PID|%JOB ...   or: kill -l [SIGNAL]"
    if args and args[0] == "-l":
        return _list_signals(args[1:], stdout, stderr)
    sig = signal.SIGTERM
    rest = list(args)
    if rest and rest[0] in ("-s", "-n"):
        if len(rest) < 2:
            print(usage, file=stderr)
            return 2
        parsed = parse_signal(rest[1])
        if parsed is None:
            print(f"kill: {rest[1]}: invalid signal specification", file=stderr)
            return 1
        sig, rest = parsed, rest[2:]
    elif rest and rest[0].startswith("-") and len(rest[0]) > 1 and rest[0] != "--":
        parsed = parse_signal(rest[0][1:])
        if parsed is None:
            print(f"kill: {rest[0][1:]}: invalid signal specification", file=stderr)
            return 1
        sig, rest = parsed, rest[1:]
    if rest and rest[0] == "--":
        rest = rest[1:]
    if not rest:
        print(usage, file=stderr)
        return 2
    status = 0
    for target in rest:
        if target.startswith("%"):
            status |= _kill_job(state, target, sig, stderr)
            continue
        if not target.isdigit():
            print(f"kill: {target}: arguments must be process or job IDs", file=stderr)
            status = 1
            continue
        try:
            _send(int(target), int(sig))
        except ProcessLookupError:
            print(f"kill: ({target}) - No such process", file=stderr)
            status = 1
        except PermissionError:
            print(f"kill: ({target}) - Operation not permitted", file=stderr)
            status = 1
        except OSError as e:
            print(f"kill: ({target}) - {e.strerror or e}", file=stderr)
            status = 1
    return status


def _kill_job(state: ShellState, spec: str, sig: int, stderr: TextIO) -> int:
    if compat.HAS_FORK:
        try:
            job = state.jobs.find(spec)
        except Exception as e:
            print(f"kill: {e}", file=stderr)
            return 1
        try:
            os.killpg(job.pgid, int(sig))
            if int(sig) in (int(signal.SIGTERM), int(signal.SIGHUP)):
                os.killpg(job.pgid, signal.SIGCONT)  # a stopped job must wake to die
            return 0
        except OSError:
            pass
        failed = 0
        for pid in job.pids:  # the group may be gone while a member is not
            try:
                os.kill(pid, int(sig))
            except OSError:
                failed += 1
        if failed == len(job.pids):
            print(f"kill: {spec}: No such process", file=stderr)
            return 1
        return 0
    text = spec[1:]
    jobs = state.background_jobs
    if not text.isdigit() or not 1 <= int(text) <= len(jobs):
        print(f"kill: {spec}: no such job", file=stderr)
        return 1
    try:
        jobs[int(text) - 1].terminate()
    except OSError as e:
        print(f"kill: {spec}: {e.strerror or e}", file=stderr)
        return 1
    return 0


BUILTINS = {"kill": builtin_kill}
PORTABLE = {"ps": builtin_ps, "top": builtin_top}
