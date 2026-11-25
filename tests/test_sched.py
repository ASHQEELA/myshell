import datetime as dt
import io
import json
import time

import pytest

from myshell import sched
from myshell.errors import ShellError

NOW = dt.datetime(2026, 11, 24, 16, 30)  # a Tuesday


def run(fn, state, args):
    out, err = io.StringIO(), io.StringIO()
    code = fn(state, args, out, err)
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("expr,expected", [
    ("*/5 * * * *", dt.datetime(2026, 11, 24, 16, 35)),
    ("0 9 * * 1-5", dt.datetime(2026, 11, 25, 9, 0)),
    ("@daily", dt.datetime(2026, 11, 25, 0, 0)),
    ("30 4 1,15 * *", dt.datetime(2026, 12, 1, 4, 30)),
    ("0 0 * * mon", dt.datetime(2026, 11, 30, 0, 0)),
    ("0 0 1 jan *", dt.datetime(2027, 1, 1, 0, 0)),
    ("0 8 * * 7", dt.datetime(2026, 11, 29, 8, 0)),
    ("@hourly", dt.datetime(2026, 11, 24, 17, 0)),
    ("0 0 29 feb *", dt.datetime(2028, 2, 29, 0, 0)),
])
def test_next_run(expr, expected):
    assert sched.CronSpec(expr).next_after(NOW) == expected


@pytest.mark.parametrize("bad", ["61 * * * *", "* * *", "a * * * *", "*/0 * * * *",
                                 "* 24 * * *", "* * 0 * *", "5-1 * * * *", "* * * 13 *"])
def test_bad_expressions(bad):
    with pytest.raises(sched.CronError):
        sched.CronSpec(bad)


def test_day_and_weekday_both_set_means_either():
    spec = sched.CronSpec("0 0 1 * mon")
    assert spec.matches(dt.datetime(2026, 12, 1, 0, 0))   # the 1st, a Tuesday
    assert spec.matches(dt.datetime(2026, 11, 30, 0, 0))  # a Monday
    assert not spec.matches(dt.datetime(2026, 12, 2, 0, 0))


@pytest.mark.parametrize("args,when,rest", [
    (["17:30", "echo", "hi"], dt.datetime(2026, 11, 24, 17, 30), ["echo", "hi"]),
    (["5pm", "x"], dt.datetime(2026, 11, 24, 17, 0), ["x"]),
    (["5", "pm", "x"], dt.datetime(2026, 11, 24, 17, 0), ["x"]),
    (["12am", "x"], dt.datetime(2026, 11, 25, 0, 0), ["x"]),
    (["16:00", "x"], dt.datetime(2026, 11, 25, 16, 0), ["x"]),
    (["+10m", "x", "y"], dt.datetime(2026, 11, 24, 16, 40), ["x", "y"]),
    (["+2", "hours", "x"], dt.datetime(2026, 11, 24, 18, 30), ["x"]),
    (["now", "+", "2", "hours", "x"], dt.datetime(2026, 11, 24, 18, 30), ["x"]),
    (["now", "+10", "min", "x"], dt.datetime(2026, 11, 24, 16, 40), ["x"]),
    (["now", "+1d", "x"], dt.datetime(2026, 11, 25, 16, 30), ["x"]),
    (["2026-12-01", "09:30", "run"], dt.datetime(2026, 12, 1, 9, 30), ["run"]),
])
def test_parse_at(args, when, rest):
    assert sched.parse_at(args, NOW) == (when, rest)


@pytest.mark.parametrize("args", [[], ["tomorrowish", "x"], ["25:00", "x"], ["+5parsecs", "x"],
                                  ["2026-02-30", "09:00", "x"], ["13pm", "x"]])
def test_parse_at_errors(args):
    with pytest.raises(ShellError):
        sched.parse_at(args, NOW)


def test_cron_add_list_remove(state):
    code, out, _ = run(sched.builtin_cron, state, ["add", "*/5 * * * *", "echo", "hi"])
    assert code == 0 and "cron #1 added" in out
    code, out, _ = run(sched.builtin_cron, state, ["add", "0", "9", "*", "*", "1-5", "echo", "work"])
    assert code == 0 and "#2" in out
    code, out, _ = run(sched.builtin_cron, state, [])
    assert "echo hi" in out and "echo work" in out
    code, out, _ = run(sched.builtin_cron, state, ["rm", "1"])
    assert code == 0
    assert "echo hi" not in run(sched.builtin_cron, state, [])[1]
    assert run(sched.builtin_cron, state, ["rm", "9"])[0] == 1


def test_cron_add_validates(state):
    code, _, err = run(sched.builtin_cron, state, ["add", "99 * * * *", "echo"])
    assert code == 2 and "minute" in err
    assert run(sched.builtin_cron, state, ["add", "@daily"])[0] == 2
    assert run(sched.builtin_cron, state, ["add", "* *"])[0] == 2
    assert run(sched.builtin_cron, state, ["bogus"])[0] == 2


def test_cron_check_and_next(state):
    code, out, _ = run(sched.builtin_cron, state, ["check", "*/30", "*", "*", "*", "*"])
    assert code == 0 and out.startswith("OK")
    assert run(sched.builtin_cron, state, ["check", "nope"])[0] == 1
    run(sched.builtin_cron, state, ["add", "@hourly", "echo", "x"])
    assert "#1 echo x:" in run(sched.builtin_cron, state, ["next"])[1]


def test_tick_runs_due_cron_once_per_minute(state, monkeypatch):
    started = []

    class Fake:
        def poll(self):
            return 0
    monkeypatch.setattr(sched, "_spawn", lambda cmd, label: started.append((cmd, label)) or Fake())
    run(sched.builtin_cron, state, ["add", "*/5 * * * *", "echo", "hi"])
    s = sched.Scheduler()
    at = dt.datetime(2026, 11, 24, 16, 35, 10)
    assert len(s.tick(at)) == 1
    assert s.tick(at + dt.timedelta(seconds=20)) == []  # same minute
    assert s.tick(dt.datetime(2026, 11, 24, 16, 36)) == []  # not due
    assert len(s.tick(dt.datetime(2026, 11, 24, 16, 40))) == 1
    assert started == [("echo hi", "cron #1")] * 2
    assert len(s.take_notices()) == 2 and s.take_notices() == []


def test_at_runs_once_and_is_removed(state, monkeypatch):
    started = []

    class Fake:
        def poll(self):
            return 0
    monkeypatch.setattr(sched, "_spawn", lambda cmd, label: started.append(cmd) or Fake())
    code, out, _ = run(sched.builtin_at, state, ["+1m", "echo", "later"])
    assert code == 0 and "at #1" in out
    assert "echo later" in run(sched.builtin_at, state, ["-l"])[1]
    s = sched.Scheduler()
    assert s.tick() == []
    assert len(s.tick(dt.datetime.now() + dt.timedelta(minutes=2))) == 1
    assert s.tick(dt.datetime.now() + dt.timedelta(minutes=3)) == []
    assert started == ["echo later"]
    assert "No waiting" in run(sched.builtin_at, state, ["-l"])[1]


def test_at_remove_and_errors(state):
    run(sched.builtin_at, state, ["+5m", "echo", "x"])
    assert run(sched.builtin_at, state, ["-r", "1"])[0] == 0
    assert run(sched.builtin_at, state, ["-r", "1"])[0] == 1
    assert run(sched.builtin_at, state, ["+5m"])[0] == 2
    assert run(sched.builtin_at, state, ["lunch", "x"])[0] == 2


def test_corrupt_data_file_is_ignored(state, dirs):
    home, _ = dirs
    (home / ".myshell_sched.json").write_text("{not json")
    assert sched.load() == {"cron": [], "at": [], "next_id": 1}


def test_real_task_writes_the_log(state, dirs, monkeypatch):
    home, _ = dirs
    monkeypatch.setenv("PYTHONPATH", __import__("os").path.dirname(
        __import__("os").path.dirname(__file__)))
    monkeypatch.setenv("TERM", "dumb")
    run(sched.builtin_cron, state, ["add", "* * * * *", "echo", "from-cron-task"])
    s = sched.Scheduler()
    assert len(s.tick(dt.datetime.now())) == 1
    log = home / ".myshell_cron.log"
    deadline = time.time() + 20
    while time.time() < deadline and "from-cron-task" not in (
            log.read_text() if log.exists() else ""):
        time.sleep(0.1)
    assert "from-cron-task" in log.read_text()
    code, out, _ = run(sched.builtin_cron, state, ["log"])
    assert "from-cron-task" in out
    assert run(sched.builtin_cron, state, ["log", "--clear"])[0] == 0
    assert not log.exists()


def test_shell_commands(sh):
    assert sh("cron add '*/5 * * * *' echo hi\ncron list").out.count("echo hi") == 1
    r = sh("at +5m echo hello\nat -l")
    assert "echo hello" in r.out
    data = json.loads((sh.home / ".myshell_sched.json").read_text())
    assert data["at"][0]["cmd"] == "echo hello"
