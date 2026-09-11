"""Running stack commands: no shell, both streams, and a clip on the size."""

from __future__ import annotations

from pathlib import Path

import pytest

from applace.shell import MAX_OUTPUT, CommandNotFound, run


def test_a_command_reports_its_output_and_success(tmp_path: Path) -> None:
    result = run("echo hello", cwd=tmp_path)
    assert result.ok
    assert result.output.strip() == "hello"
    assert result.duration_ms >= 0


def test_a_failing_command_is_a_result_not_an_exception(tmp_path: Path) -> None:
    result = run("false", cwd=tmp_path)
    assert not result.ok
    assert result.code != 0


def test_both_streams_are_captured(tmp_path: Path) -> None:
    """npm warns on one and errors on the other; a parser has to see both."""
    result = run("sh -c 'echo out; echo err >&2'", cwd=tmp_path)
    assert "out" in result.output and "err" in result.output


def test_there_is_no_shell(tmp_path: Path) -> None:
    """A stack file must not be able to smuggle a pipeline past us."""
    marker = tmp_path / "pwned"
    result = run(f"echo hi > {marker}", cwd=tmp_path)
    assert not marker.exists()
    assert result.output.strip() == f"hi > {marker}"


def test_a_missing_program_says_which_one(tmp_path: Path) -> None:
    with pytest.raises(CommandNotFound) as exc:
        run("applace-no-such-program", cwd=tmp_path)
    assert "applace-no-such-program" in str(exc.value)


def test_a_timeout_comes_back_as_a_result(tmp_path: Path) -> None:
    result = run("sleep 5", cwd=tmp_path, timeout=0.2)
    assert result.timed_out
    assert not result.ok


def test_a_huge_output_keeps_its_end_where_the_error_is(tmp_path: Path) -> None:
    script = f"for i in $(seq 1 {MAX_OUTPUT}); do echo LINE$i; done; echo THE_ERROR"
    result = run(f"sh -c '{script}'", cwd=tmp_path)
    assert len(result.stdout) <= MAX_OUTPUT + 200
    assert "THE_ERROR" in result.output
    assert "characters omitted" in result.output
