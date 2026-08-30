from agent.sandbox import run_patch


def test_run_patch_success():
    result = run_patch("def valid():\n    return 1\n")
    assert result.accepted is True
    assert result.returncode == 0
    assert result.stderr == ""


def test_run_patch_failure():
    result = run_patch("def broken(:\n    pass\n")
    assert result.accepted is False
    assert result.returncode == 1
    assert "SyntaxError" in result.stderr


def test_run_patch_does_not_execute_code():
    # The syntax gate must not run generated code; Docker validation is the
    # separate execution stage.
    result = run_patch("raise RuntimeError('would fail if executed')")
    assert result.accepted is True
