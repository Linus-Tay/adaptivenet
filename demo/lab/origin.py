"""Demo origin: the existing demo catalogue plus lab-only metrics and app faults.

Wraps ``adaptivenet.demo_api`` without changing it. ``/lab/metrics`` reports
values measured inside this container (process CPU, cgroup/proc memory, load,
request counters). It is the local stand-in for the AMP scrape of an EC2 origin
and is labelled as such everywhere downstream. App faults here are visible to
the internal (AWS-like) view; network faults live on the gateway instead.
"""
from __future__ import annotations

from collections import deque
import os
from pathlib import Path
import secrets
import threading
import time

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from adaptivenet.demo_api import app

REGION = os.getenv("REGION", "region-a")
WINDOW_SEC = 60
_requests: deque[tuple[float, int, float]] = deque(maxlen=20_000)
_fault: dict = {}
_lock = threading.Lock()


class AppFault(BaseModel):
    kind: str = Field(pattern="^(app_5xx|down|cpu_burn)$")
    error_rate: float = Field(default=1.0, ge=0, le=1)
    duration_seconds: int = Field(default=120, ge=1, le=900)


def _authorize(token: str | None) -> None:
    expected = os.environ.get("LAB_TOKEN", "")
    if not expected or not secrets.compare_digest(token or "", expected):
        raise HTTPException(401, "Lab token required")


def _active_fault(now: float | None = None) -> dict | None:
    now = time.monotonic() if now is None else now
    with _lock:
        if _fault and _fault["expires"] > now:
            return dict(_fault)
    return None


@app.middleware("http")
async def lab_fault_and_counters(request: Request, call_next):
    path = request.url.path
    if path.startswith("/lab/"):
        return await call_next(request)
    started = time.monotonic()
    fault = _active_fault(started)
    import random
    if fault and (fault["kind"] == "down" or (
            fault["kind"] == "app_5xx" and path != "/healthz" and random.random() < fault["error_rate"])):
        response = JSONResponse({"region": REGION, "error": f"lab {fault['kind']} fault"}, status_code=503)
    else:
        response = await call_next(request)
    if path == "/api/catalog":
        _requests.append((time.time(), response.status_code, (time.monotonic() - started) * 1000))
    return response


def _burn(until: float) -> None:
    while time.monotonic() < until and _active_fault():
        sum(i * i for i in range(10_000))


@app.post("/lab/app-fault")
def inject(body: AppFault, x_lab_token: str | None = Header(default=None)):
    _authorize(x_lab_token)
    expires = time.monotonic() + body.duration_seconds
    with _lock:
        _fault.clear()
        _fault.update(kind=body.kind, error_rate=body.error_rate, expires=expires,
                      duration_seconds=body.duration_seconds)
    if body.kind == "cpu_burn":
        threading.Thread(target=_burn, args=(expires,), daemon=True).start()
    return {"region": REGION, "status": "active", **body.model_dump()}


@app.delete("/lab/app-fault")
def clear(x_lab_token: str | None = Header(default=None)):
    _authorize(x_lab_token)
    with _lock:
        _fault.clear()
    return {"region": REGION, "status": "cleared"}


_cpu_prev: tuple[float, float] | None = None


def _process_cpu_pct() -> float | None:
    """CPU share of one core used by this process since the previous scrape."""
    global _cpu_prev
    try:
        ticks = os.sysconf("SC_CLK_TCK")
        fields = Path("/proc/self/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        used = (int(fields[11]) + int(fields[12])) / ticks
    except (OSError, ValueError, IndexError, AttributeError):  # no /proc or sysconf (macOS, Windows)
        times = os.times()
        used = times.user + times.system
    now = time.monotonic()
    previous, _cpu_prev = _cpu_prev, (now, used)
    if previous is None or now <= previous[0]:
        return None  # first scrape has no interval: unknown, not zero
    return round(min(100.0, max(0.0, 100 * (used - previous[1]) / (now - previous[0]))), 3)


def _memory() -> tuple[float | None, float | None]:
    """(available, total) bytes from cgroup v2 limit if set, else /proc/meminfo."""
    try:
        limit = Path("/sys/fs/cgroup/memory.max").read_text(encoding="utf-8").strip()
        current = int(Path("/sys/fs/cgroup/memory.current").read_text(encoding="utf-8"))
        if limit != "max":
            total = int(limit)
            return float(max(0, total - current)), float(total)
    except (OSError, ValueError):
        pass
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0]) * 1024
        return float(info["MemAvailable"]), float(info["MemTotal"])
    except (OSError, KeyError, ValueError, IndexError):
        pass
    return _windows_memory() if os.name == "nt" else _darwin_memory()


def _windows_memory() -> tuple[float | None, float | None]:
    """Native Windows runs (smoke test): GlobalMemoryStatusEx."""
    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    try:
        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None, None
        return float(status.ullAvailPhys), float(status.ullTotalPhys)
    except (OSError, AttributeError):
        return None, None


def _darwin_memory() -> tuple[float | None, float | None]:
    """Native macOS runs (smoke test): host memory from sysconf + vm_stat."""
    import subprocess
    try:
        page = os.sysconf("SC_PAGE_SIZE")
        total = float(page * os.sysconf("SC_PHYS_PAGES"))
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2).stdout
        pages = {}
        for line in out.splitlines()[1:]:
            key, _, value = line.partition(":")
            pages[key.strip()] = int(value.strip().rstrip("."))
        free = pages["Pages free"] + pages.get("Pages inactive", 0) + pages.get("Pages speculative", 0)
        return float(free * page), total
    except (OSError, ValueError, KeyError, AttributeError, subprocess.SubprocessError):
        return None, None


def _load1() -> float | None:
    try:
        return float(os.getloadavg()[0])
    except (OSError, AttributeError):  # Windows has no load average: unknown
        return None


@app.get("/lab/metrics")
def metrics():
    """Measured values only. Unknown values are null, never zero."""
    now = time.time()
    window = [r for r in list(_requests) if now - r[0] <= WINDOW_SEC]
    latencies = sorted(r[2] for r in window)
    p95 = None
    if latencies:
        idx = round(0.95 * (len(latencies) - 1))
        p95 = round(latencies[idx], 3)
    available, total = _memory()
    fault = _active_fault()
    return {
        "region": REGION,
        "observed_at": now,
        "source": "local_lab_origin",
        "app_up": 0 if fault and fault["kind"] == "down" else 1,
        "cpu_busy_pct": _process_cpu_pct(),
        "memory_available_bytes": available,
        "memory_total_bytes": total,
        "node_load1": _load1(),
        # Rates need traffic: with no requests in the window they are unknown.
        "http_rps": round(len(window) / WINDOW_SEC, 4) if window else None,
        "http_5xx_rps": round(sum(1 for r in window if r[1] >= 500) / WINDOW_SEC, 4) if window else None,
        "http_p95_ms": p95,
        "active_fault": None if fault is None else {k: fault[k] for k in ("kind", "error_rate", "duration_seconds")},
    }
