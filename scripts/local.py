"""Run the five local Python processes. Ctrl+C cleans them up."""
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]

def environment():
    env = os.environ.copy()
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env.setdefault(key.strip(), value.strip())
    if not env.get("LAB_TOKEN"):
        raise SystemExit("Copy .env.example to .env and set LAB_TOKEN first.")
    env["PYTHONPATH"] = str(ROOT / "services")
    return env

def start(env):
    processes = []
    try:
        for module, port, region in [("demo_api",8101,"region-a"),("demo_api",8102,"region-b"),
                                     ("gateway",8080,""),("control_api",8000,""),("experiment_runner",8001,"")]:
            processes.append(subprocess.Popen([sys.executable,"-m","uvicorn",f"adaptivenet.{module}:app",
                "--host","127.0.0.1","--port",str(port)],cwd=ROOT,env={**env,"REGION":region}))
        return processes
    except BaseException:
        stop(processes)
        raise

def stop(processes):
    for process in processes:
        process.terminate()
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

if __name__ == "__main__":
    processes = start(environment())
    try:
        while all(p.poll() is None for p in processes):
            time.sleep(.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop(processes)
