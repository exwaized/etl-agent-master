import subprocess
from unittest.mock import patch, MagicMock
from agent.sandbox import run_patch


def _mock_run(returncode: int):
    result = MagicMock()
    result.returncode = returncode
    return result


def test_run_patch_success():
    with patch("agent.sandbox.subprocess.run", return_value=_mock_run(0)) as mock:
        assert run_patch("print('ok')") is True
        mock.assert_called_once()


def test_run_patch_failure():
    with patch("agent.sandbox.subprocess.run", return_value=_mock_run(1)):
        assert run_patch("raise SystemExit(1)") is False


def test_run_patch_timeout():
    with patch("agent.sandbox.subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 20)):
        assert run_patch("import time; time.sleep(999)") is False


def test_run_patch_docker_flags():
    with patch("agent.sandbox.subprocess.run", return_value=_mock_run(0)) as mock:
        run_patch("x = 1")
        cmd = mock.call_args[0][0]

    assert "docker" in cmd
    assert "--network" in cmd and "none" in cmd
    assert "--memory" in cmd and "256m" in cmd
    assert "--memory-swap" in cmd and "256m" in cmd
    assert "--read-only" in cmd


def test_run_patch_temp_file_cleaned_up(tmp_path):
    created_paths = []

    real_run = subprocess.run

    def capturing_run(cmd, **kwargs):
        volume_arg = next(a for a in cmd if ":/patch.py" in a)
        created_paths.append(volume_arg.split(":")[0])
        return _mock_run(0)

    with patch("agent.sandbox.subprocess.run", side_effect=capturing_run):
        run_patch("pass")

    assert created_paths, "no temp file path captured"
    import os
    assert not os.path.exists(created_paths[0]), "temp file was not deleted"


def test_run_patch_custom_timeout():
    with patch("agent.sandbox.subprocess.run", return_value=_mock_run(0)) as mock:
        run_patch("pass", timeout=5)
        assert mock.call_args[1]["timeout"] == 5
