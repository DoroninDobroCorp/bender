import subprocess

import cli.bender_cli as bender_cli


def test_should_skip_global_cleanup_when_other_bender_cli_process_exists(monkeypatch):
    monkeypatch.delenv("BENDER_ALLOW_PARALLEL", raising=False)
    monkeypatch.delenv("BENDER_SKIP_GLOBAL_CLEANUP", raising=False)
    monkeypatch.setattr(bender_cli.os, "getpid", lambda: 111)

    def fake_run(cmd, capture_output=True, text=True, timeout=2):
        # _should_run_global_cleanup uses pgrep -f "bender run"
        if cmd[:2] == ["pgrep", "-f"]:
            # Return two PIDs: current (111) and another (222)
            return subprocess.CompletedProcess(cmd, 0, "111\n222\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr(bender_cli.subprocess, "run", fake_run)

    assert bender_cli._should_run_global_cleanup() is False


def test_should_allow_global_cleanup_when_only_current_process_exists(monkeypatch):
    monkeypatch.delenv("BENDER_ALLOW_PARALLEL", raising=False)
    monkeypatch.delenv("BENDER_SKIP_GLOBAL_CLEANUP", raising=False)
    monkeypatch.setattr(bender_cli.os, "getpid", lambda: 333)

    def fake_run(cmd, capture_output=True, text=True, timeout=2):
        if cmd[:2] == ["pgrep", "-f"]:
            # Only current PID found
            return subprocess.CompletedProcess(cmd, 0, "333\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr(bender_cli.subprocess, "run", fake_run)

    assert bender_cli._should_run_global_cleanup() is True
