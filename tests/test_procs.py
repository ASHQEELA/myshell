import io
import os
import signal
import subprocess
import sys

import pytest

from myshell import procs
from conftest import unix_only


def run(fn, state, args):
    out, err = io.StringIO(), io.StringIO()
    code = fn(state, args, out, err)
    return code, out.getvalue(), err.getvalue()


def test_parse_tasklist():
    text = '"System Idle Process","0","Services","0","8 K"\n"python.exe","4242","Console","1","23,456 K"\n'
    found = procs.parse_tasklist(text)
    assert [(p.pid, p.name, p.rss_kb, p.ppid) for p in found] == [
        (0, "System Idle Process", 8, None), (4242, "python.exe", 23456, None)]


def test_parse_tasklist_skips_junk():
    assert procs.parse_tasklist("INFO: No tasks are running\n") == []


def test_parse_ps_output():
    text = "    1     0  5000 Ss   /sbin/launchd\n  200     1   900 R+   bash\nbad line\n"
    found = procs.parse_ps_output(text)
    assert [(p.pid, p.ppid, p.rss_kb, p.state, p.name) for p in found] == [
        (1, 0, 5000, "Ss", "launchd"), (200, 1, 900, "R+", "bash")]


def test_descendants_follow_parents():
    tree = [procs.Proc(1, 0, "init", 1), procs.Proc(2, 1, "a", 1), procs.Proc(3, 2, "b", 1),
            procs.Proc(4, 1, "c", 1), procs.Proc(5, 99, "other", 1)]
    assert sorted(p.pid for p in procs.descendants(tree, 2)) == [2, 3]


def test_cpu_percent():
    before = [procs.Proc(1, 0, "x", 1, ticks=100)]
    after = [procs.Proc(1, 0, "x", 1, ticks=150)]
    pct = procs.cpu_percent(before, after, 1.0)
    assert pct[1] == pytest.approx(50 / procs._clock_ticks() * 100)


def test_ps_lists_own_process(state, monkeypatch):
    fake = [procs.Proc(os.getpid(), 1, "myshell", 2048, "R"), procs.Proc(7, 1, "elsewhere", 10)]
    monkeypatch.setattr(procs, "list_processes", lambda: fake)
    code, out, _ = run(procs.builtin_ps, state, [])
    assert code == 0 and "myshell" in out and "elsewhere" not in out
    code, out, _ = run(procs.builtin_ps, state, ["-e"])
    assert "elsewhere" in out


def test_ps_rejects_unknown_option(state):
    code, _, err = run(procs.builtin_ps, state, ["--nope"])
    assert code == 2 and "unknown option" in err


def test_top_one_frame(state, monkeypatch):
    monkeypatch.setattr(procs, "_sleep", lambda s: None)
    fake = [procs.Proc(1, 0, "big", 9000, ticks=1), procs.Proc(2, 0, "small", 5, ticks=1)]
    monkeypatch.setattr(procs, "list_processes", lambda: fake)
    code, out, _ = run(procs.builtin_top, state, ["-n", "1", "-s", "mem"])
    assert code == 0
    assert out.index("big") < out.index("small")


def test_top_bad_usage(state):
    for bad in (["-n"], ["-n", "x"], ["-s", "disk"], ["--what"], ["-l", "0"]):
        assert run(procs.builtin_top, state, bad)[0] == 2


def test_kill_list_names(state):
    code, out, _ = run(procs.builtin_kill, state, ["-l"])
    assert code == 0 and "TERM" in out and "KILL" in out or os.name == "nt"
    assert run(procs.builtin_kill, state, ["-l", "9"])[1].strip() in ("KILL", "")
    assert run(procs.builtin_kill, state, ["-l", "TERM"])[1].strip() == str(int(signal.SIGTERM))


def test_kill_usage_and_bad_signal(state):
    assert run(procs.builtin_kill, state, [])[0] == 2
    code, _, err = run(procs.builtin_kill, state, ["-s", "NOPE", "1"])
    assert code == 1 and "invalid signal" in err
    code, _, err = run(procs.builtin_kill, state, ["abc"])
    assert code == 1 and "process or job" in err


def test_kill_missing_process(state):
    code, _, err = run(procs.builtin_kill, state, ["-0", "999999"])
    assert code == 1 and "No such process" in err


def test_kill_stops_a_process(state):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        code, _, _ = run(procs.builtin_kill, state, [str(p.pid)])
        assert code == 0
        assert p.wait(timeout=10) != 0
    finally:
        if p.poll() is None:
            p.kill()


@unix_only
def test_kill_with_signal_name_and_number(state):
    for flag in ("-KILL", "-9", "-SIGKILL"):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            assert run(procs.builtin_kill, state, [flag, str(p.pid)])[0] == 0
            assert p.wait(timeout=10) == -9
        finally:
            if p.poll() is None:
                p.kill()


@unix_only
def test_kill_job_spec(state):
    from types import SimpleNamespace
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                         start_new_session=True)
    try:
        state.jobs = SimpleNamespace(find=lambda spec: SimpleNamespace(pgid=p.pid))
        assert run(procs.builtin_kill, state, ["%1"])[0] == 0
        assert p.wait(timeout=10) != 0
    finally:
        if p.poll() is None:
            p.kill()


def test_kill_unknown_job(state):
    code, _, err = run(procs.builtin_kill, state, ["%9"])
    assert code == 1 and "kill:" in err


def test_ps_and_kill_through_the_shell(sh):
    assert sh("kill").code == 2
    assert "usage" in sh("kill").err
    r = sh("ps -e")
    assert r.code == 0 and "PID" in r.out


@unix_only
def test_real_process_listing_finds_this_test():
    found = procs.list_processes()
    mine = [p for p in found if p.pid == os.getpid()]
    assert mine and mine[0].rss_kb > 0
