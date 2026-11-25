

def test_script_gets_arguments_and_skips_shebang(sh, run_cli, dirs):
    _, work = dirs
    (work / "t.msh").write_text('#!/usr/bin/env myshell\necho "$0 has $# args: $1 $2"\n')
    r = run_cli(["t.msh", "a", "b"])
    assert r.code == 0 and r.out.strip() == "t.msh has 2 args: a b"


def test_script_exit_status_and_missing_file(sh, run_cli, dirs):
    _, work = dirs
    (work / "bad.msh").write_text("exit 7\n")
    assert run_cli(["bad.msh"]).code == 7
    r = run_cli(["nope.msh"])
    assert r.code == 127 and "nope.msh" in r.err


def test_script_syntax_error_names_the_line(sh, run_cli, dirs):
    _, work = dirs
    (work / "s.msh").write_text('echo ok\necho "open\n')
    r = run_cli(["s.msh"])
    assert "ok" in r.out and "s.msh" in r.err


def test_version_and_help(run_cli):
    r = run_cli(["--version"])
    assert r.code == 0 and r.out.startswith("myshell ")
    r = run_cli(["--help"])
    assert r.code == 0 and "--norc" in r.out


def test_rcfile_option_needs_value(run_cli):
    assert run_cli(["--rcfile"]).code == 2


def test_rc_file_norc_rcfile_and_env(run_cli, dirs):
    home, work = dirs
    (home / ".myshellrc").write_text("echo from-default\n")
    (work / "other.rc").write_text("echo from-other\n")
    assert "from-default" in run_cli([]).out
    assert "from-default" not in run_cli(["--norc"]).out
    r = run_cli(["--rcfile", str(work / "other.rc")])
    assert "from-other" in r.out and "from-default" not in r.out
    r = run_cli([], env={"MYSHELLRC": str(work / "other.rc")})
    assert "from-other" in r.out and "from-default" not in r.out


def test_rc_error_does_not_stop_the_shell(run_cli, dirs):
    home, _ = dirs
    (home / ".myshellrc").write_text('echo "open\necho after\n')
    r = run_cli([])
    assert r.code == 0
