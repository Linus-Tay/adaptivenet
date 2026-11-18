"""The cross-platform entry script: commands map to the expected calls (nothing is executed)."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("run_entry", Path(__file__).resolve().parents[1] / "run.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


def test_help_and_unknown_command():
    assert entry.main(["--help"]) == 0
    assert entry.main(["nope"]) == 2


def test_live_uses_direct_mode_with_witness(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(entry, "need_venv", lambda: "python")
    monkeypatch.setattr(entry, "run", lambda cmd, **kw: calls.append((cmd, kw)) or 0)
    monkeypatch.setattr(entry.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})())
    assert entry.main(["live", "--force-ipv4"]) == 0
    cmd, kw = calls[-1]
    assert "--sqs" not in cmd and "--traffic" in cmd
    assert cmd[cmd.index("--profile") + 1] == "adaptivenet-admin"
    assert "ipv4" in kw["env"]["PYTHONPATH"]


def test_compose_uses_lab_files(monkeypatch):
    calls = []
    monkeypatch.setattr(entry, "docker", lambda: "docker")
    monkeypatch.setattr(entry, "demo_files", lambda: None)
    monkeypatch.setattr(entry, "run", lambda cmd, **kw: calls.append(cmd) or 0)
    entry.main(["demo-down"])
    assert calls[-1][:6] == ["docker", "compose", "-f", "compose.yaml", "-f", "demo/compose.lab-overlay.yaml"]
