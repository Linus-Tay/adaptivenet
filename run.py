#!/usr/bin/env python3
"""One entry point for AdaptiveNet on macOS, Linux and Windows (plain PowerShell).

  python run.py check        are the prerequisites installed and running?
  python run.py setup        create .venv and install everything (add --cloud for live AWS tools)
  python run.py test         run all Python tests, then the no-Docker smoke test
  python run.py demo-up      start the Docker demo (dashboard :3000, cafe site :3001)
  python run.py demo-reset   clear faults, 100% Singapore, fresh controller journal
  python run.py demo-down    stop the Docker demo
  python run.py fault lab-latency --region sg --ms 1500    (any demo.faultctl command)
  python run.py live         real AWS telemetry on :8766 (needs AWS sign-in; see docs/ENV_SETUP.txt)

On macOS/Linux use python3 if "python" is not found. Only the standard library is used
here, so this file runs before setup.
"""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
WINDOWS = os.name == "nt"
VENV = ROOT / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if WINDOWS else "bin/python")
ENV_DEMO = ROOT / "demo" / ".env.demo"
DEMO_PORTS = (3000, 3001, 8000, 8080, 8101, 8102, 8192)
SUITES = ["tests", "demo/tests", "ml/team_starter/tests", "step7_finish", "step8_finish"]
MAC_DOCKER = "/Applications/Docker.app/Contents/Resources/bin/docker"


def say(ok: bool | None, text: str) -> None:
    mark = {True: "OK  ", False: "FIX ", None: "--  "}[ok]
    print(f"  {mark}{text}")


def py_env(extra: dict | None = None) -> dict:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "services"), str(ROOT)])}
    env.update(extra or {})
    return env


def docker() -> str | None:
    found = shutil.which("docker")
    if not found and Path(MAC_DOCKER).exists():
        found = MAC_DOCKER
    return found


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def run(cmd: list[str], **kw) -> int:
    print("$", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.call(cmd, cwd=ROOT, **kw)


def need_venv() -> str:
    if not VENV_PY.exists():
        sys.exit("No .venv yet. Run:  python run.py setup")
    return str(VENV_PY)


# ---------------------------------------------------------------- commands

PY_FIX = ("install Python 3.14 (64-bit) from https://www.python.org/downloads/ "
          "(on Windows: the 'Windows installer (64-bit)'), then run this again with that Python")


def python_problem() -> str | None:
    """The pinned scikit-learn, numpy and scipy have ready-made packages only for 64-bit Python 3.11 to 3.14."""
    v = sys.version_info
    if v < (3, 11):
        return f"Python {v[0]}.{v[1]} is too old"
    if v >= (3, 15):
        return f"Python {v[0]}.{v[1]} is too new for the pinned ML packages"
    if sys.maxsize <= 2**32:
        return "this Python is 32-bit; the ML packages need 64-bit"
    return None


def check() -> int:
    print("Prerequisites")
    problems = 0
    issue = python_problem()
    say(issue is None, f"Python {sys.version.split()[0]}" + (f": {issue}  -> {PY_FIX}" if issue else ""))
    problems += issue is not None
    d = docker()
    if not d:
        say(False, "Docker not found  -> install Docker Desktop: https://www.docker.com/products/docker-desktop/")
        problems += 1
    else:
        running = subprocess.call([d, "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
        say(running, "Docker is running" if running else "Docker is installed but not running  -> open Docker Desktop and wait for it to start")
        problems += not running
        compose = subprocess.call([d, "compose", "version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
        say(compose, "Docker Compose v2" if compose else "docker compose not available  -> update Docker Desktop")
        problems += not compose
    say(VENV_PY.exists() or None, ".venv ready" if VENV_PY.exists() else ".venv not created yet  -> python run.py setup")
    busy = [p for p in DEMO_PORTS if not port_free(p)]
    if busy:
        say(None, f"ports in use: {busy} (fine if the demo is already running; 8080 is often LM Studio)")
    else:
        say(True, "demo ports free")
    aws = shutil.which("aws") or (Path.home() / "aws-cli" / "aws").exists()
    say(bool(aws) or None, "AWS CLI found (only for live AWS)" if aws else
        "AWS CLI not found (only needed for live AWS: https://aws.amazon.com/cli/)")
    print("All required prerequisites are present." if not problems else f"{problems} problem(s) to fix first.")
    return 1 if problems else 0


def setup(cloud: bool) -> int:
    issue = python_problem()
    if issue:
        sys.exit(f"{issue}. Fix: {PY_FIX}")
    if VENV_PY.exists():
        probe = subprocess.run([str(VENV_PY), "-c", "import sys;print(sys.version_info[:2]>=(3,11) and sys.version_info[:2]<(3,15) and sys.maxsize>2**32)"],
                               capture_output=True, text=True)
        if probe.stdout.strip() != "True":
            sys.exit("The existing .venv was made with an unsupported Python. Delete the .venv folder, then run setup again.")
    else:
        if run([sys.executable, "-m", "venv", str(VENV)]):
            return 1
    py = str(VENV_PY)
    steps = [[py, "-m", "pip", "install", "--upgrade", "pip"],
             [py, "-m", "pip", "install", "-r", "requirements-dev.lock",
              "-r", "ml/team_starter/requirements-ml-inference.lock"],
             [py, "-m", "pip", "install", "--no-deps", "-e", "."]]
    if cloud:
        steps.append([py, "-m", "pip", "install", "-e", ".[cloud]"])
    for step in steps:
        if run(step):
            return 1
    print("Setup done. Next:  python run.py test")
    return 0


def test() -> int:
    py = need_venv()
    code = run([py, "-m", "pytest", "-q", *SUITES], env=py_env())
    if code:
        return code
    return run([py, "-m", "demo.smoke_local"], env=py_env())


def demo_files() -> None:
    if not ENV_DEMO.exists():
        ENV_DEMO.write_text(f"LAB_TOKEN={secrets.token_hex(16)}\n", encoding="ascii")
    for name in ("lab-live", "demo-models"):
        folder = ROOT / "data" / name
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("linux"):
            os.chmod(folder, 0o777)  # containers run as uid 10001


def compose(*args: str) -> int:
    d = docker()
    if not d:
        sys.exit("Docker not found. Install Docker Desktop, then run:  python run.py check")
    demo_files()
    return run([d, "compose", "-f", "compose.yaml", "-f", "demo/compose.lab-overlay.yaml",
                "--env-file", str(ENV_DEMO), *args])


def demo_up() -> int:
    if not port_free(8080):
        print("Note: port 8080 is in use. Fine if the demo is already up; otherwise free it (often LM Studio).")
    code = compose("up", "--build", "-d")
    if not code:
        print("Demo dashboard: http://127.0.0.1:3000   cafe site: http://127.0.0.1:3001   debug: http://127.0.0.1:8192")
    return code


def fault(args: list[str]) -> int:
    demo_files()
    return run([need_venv() if VENV_PY.exists() else sys.executable, "-m", "demo.faultctl", *args], env=py_env())


def demo_reset() -> int:
    fault(["lab-clear"])
    compose("stop", "lab-controller")
    journal = ROOT / "data" / "lab-live" / "controller-journal.db"
    journal.unlink(missing_ok=True)
    compose("restart", "gateway")
    return compose("start", "lab-controller")


def live(args: list[str]) -> int:
    """Real AWS telemetry, direct mode (no shared SQS queue). Extra args go to live_monitor.py."""
    py = need_venv()
    extra: dict = {}
    path = os.environ.get("PATH", "")
    if (Path.home() / "aws-cli").is_dir():
        path += os.pathsep + str(Path.home() / "aws-cli")  # last: that folder ships its own python
    extra["PATH"] = os.pathsep.join([str(VENV_PY.parent), path])
    if "--force-ipv4" in args:
        args = [a for a in args if a != "--force-ipv4"]
        extra["PYTHONPATH"] = os.pathsep.join([str(ROOT / "scripts" / "live" / "ipv4"), str(ROOT / "services"), str(ROOT)])
    cert = subprocess.run([py, "-c", "import certifi;print(certifi.where())"], capture_output=True, text=True)
    if cert.returncode == 0 and not os.environ.get("SSL_CERT_FILE"):
        extra["SSL_CERT_FILE"] = cert.stdout.strip()
    if not any(a.startswith("--profile") for a in args):
        args += ["--profile", "adaptivenet-admin"]
    if "--traffic" not in args:
        args.append("--traffic")
    print("Live AWS telemetry on http://127.0.0.1:8766  (Ctrl+C stops it)")
    return run([py, "scripts/live_monitor.py", *args], env=py_env(extra))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "check":
        return check()
    if cmd == "setup":
        return setup(cloud="--cloud" in rest)
    if cmd == "test":
        return test()
    if cmd == "demo-up":
        return demo_up()
    if cmd == "demo-down":
        return compose("down")
    if cmd == "demo-reset":
        return demo_reset()
    if cmd == "status":
        return fault(["status"])
    if cmd == "fault":
        return fault(rest)
    if cmd == "live":
        return live(rest)
    print(f"Unknown command: {cmd}\n{__doc__}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
