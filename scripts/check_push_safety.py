#!/usr/bin/env python3
"""Reject accidentally staged sensitive paths and obvious credential literals.

Run before `git commit`; this is a safety net, not a substitute for reviewing diffs.
"""
from __future__ import annotations
import argparse
import re
import subprocess
import sys
from pathlib import PurePosixPath

SENSITIVE_SUFFIXES = (".tfstate", ".tfstate.backup", ".db", ".db-shm", ".db-wal", ".sqlite", ".sqlite3", ".pem", ".p12", ".pfx")
SECRET_LITERALS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(rb"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9_]{30,}"),
)


def is_sensitive_path(name: str) -> bool:
    path = PurePosixPath(name)
    parts = path.parts
    basename = path.name.lower()
    if any(p in {".aws", ".terraform", ".venv", "node_modules", "__pycache__", "data"} for p in parts):
        return True
    if basename == ".env" or (basename.startswith(".env.") and basename != ".env.example"):
        return True
    if basename in {"credentials", "id_rsa", "id_ed25519", "terraform.tfvars", "backend.hcl"}:
        return True
    if basename.endswith(SENSITIVE_SUFFIXES) or basename.endswith(".tfvars"):
        return True
    return False


def staged_paths(repo: str = ".") -> list[str]:
    result = subprocess.run(["git", "-C", repo, "diff", "--cached", "--name-only", "-z"],
                            capture_output=True, check=True)
    return [p.decode("utf-8", errors="replace") for p in result.stdout.split(b"\0") if p]


def inspect_staged(repo: str = ".") -> list[str]:
    issues = []
    for name in staged_paths(repo):
        if is_sensitive_path(name):
            issues.append(f"sensitive path staged: {name}")
            continue
        content = subprocess.run(["git", "-C", repo, "show", f":{name}"],
                                 capture_output=True, check=False)
        if content.returncode != 0:
            continue  # deletion or unmerged path
        if len(content.stdout) > 2_000_000 or b"\x00" in content.stdout[:4096]:
            continue  # binary or very large artifacts: review separately
        if any(p.search(content.stdout) for p in SECRET_LITERALS):
            issues.append(f"possible credential staged: {name}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Inspect staged (index) contents")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args()
    try:
        problems = inspect_staged(args.repo)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print("Could not inspect Git staging area:", exc, file=sys.stderr)
        return 2
    if problems:
        print("BLOCKED: review staged files before pushing:")
        for problem in problems:
            print(" -", problem)
        return 1
    print("PASS: No known sensitive paths or obvious credential literals staged.")
    print("Review the full diff; no automated scan can guarantee absence of secrets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
