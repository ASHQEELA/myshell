import os

import pytest

from myshell.textedit import (EditError, EditSession, cursor_position, find_text, line_start,
                              run_editor)

prompt_toolkit = pytest.importorskip("prompt_toolkit")
ui_test = pytest.mark.skipif(os.name == "nt", reason="the pipe input differs on Windows")


def test_find_text_wraps_and_ignores_case():
    text = "Alpha beta\nGamma BETA"
    assert find_text(text, "beta", 0) == 6
    assert find_text(text, "beta", 7) == 17
    assert find_text(text, "alpha", 5) == 0  # wrapped around
    assert find_text(text, "zzz", 0) is None
    assert find_text(text, "", 0) is None


def test_line_start_and_cursor_position():
    text = "one\ntwo\nthree"
    assert line_start(text, 1) == 0
    assert line_start(text, 3) == 8
    assert line_start(text, 4) is None
    assert line_start(text, 0) is None
    assert cursor_position(text, 9) == (3, 2)


def test_session_keeps_crlf(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"a\r\nb\r\n")
    s = EditSession(str(f))
    assert s.text == "a\nb\n"
    s.save("a\nb\nc\n")
    assert f.read_bytes() == b"a\r\nb\r\nc\r\n"


def test_session_adds_final_newline_and_reports_modified(tmp_path):
    f = tmp_path / "a.txt"
    s = EditSession(str(f))
    assert not s.exists and not s.modified("")
    assert s.modified("x")
    assert s.save("x") == 1
    assert f.read_text() == "x\n"
    assert not s.modified("x")


def test_session_refuses_directory_and_binary(tmp_path):
    with pytest.raises(EditError):
        EditSession(str(tmp_path))
    b = tmp_path / "b.bin"
    b.write_bytes(b"ab\0cd")
    with pytest.raises(EditError):
        EditSession(str(b))


def test_save_error_is_reported(tmp_path):
    s = EditSession(str(tmp_path / "missing_folder" / "a.txt"))
    with pytest.raises(EditError):
        s.save("x")


def drive(path, keys):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    with create_pipe_input() as inp:
        inp.send_text(keys)
        run_editor(str(path), input=inp, output=DummyOutput())


@ui_test
def test_edit_goto_find_type_and_save(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("one\ntwo\nthree\n")
    drive(f, "\x072\rHELLO \x17three\rX\x13\x18")
    assert f.read_text() == "one\nHELLO two\nXthree\n"


@ui_test
def test_exit_with_unsaved_changes_needs_second_press(tmp_path):
    f = tmp_path / "new.txt"
    drive(f, "abc\x18\x18")
    assert not f.exists()


@ui_test
def test_new_file_is_created_on_save(tmp_path):
    f = tmp_path / "new.txt"
    drive(f, "abc\r\x13\x18")
    assert f.read_text() == "abc\n"


@ui_test
def test_undo(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("keep\n")
    drive(f, "o\x1a\x13\x18")
    assert f.read_text() == "keep\n"


def test_edit_builtin_needs_terminal(sh):
    r = sh("edit file.txt")
    assert r.code == 1 and "needs a terminal" in r.err
    assert sh("edit").code == 2
    assert "usage" in sh("edit").err
