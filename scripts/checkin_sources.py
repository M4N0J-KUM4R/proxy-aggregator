#!/usr/bin/env python3
"""Check in the upstream proxy-list repositories.

Every source in sources.json points at a file inside a public GitHub
repository.  This script clones each of those repositories into
<work-dir>/repos/<owner>__<repo> - shallow, so it stays small - or pulls it
when it is already there, and writes <work-dir>/repos.json recording the
commit each repository is at.

The checked-out repositories are what fetch_sources.py reads the lists from,
so a list that changed upstream arrives through a git pull rather than an
anonymous HTTP GET.  The raw URL stays the fallback for a repository that
cannot be cloned (private, renamed, gone).

Usage:
  python3 checkin_sources.py --sources sources/sources.json --work-dir work
  python3 checkin_sources.py --repos-dir work/repos --jobs 8
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# A shallow clone is enough: the lists are read as files, and the commit is
# recorded for provenance.  A full history of 34 proxy-list repositories is
# tens of gigabytes and buys nothing.
CLONE_TIMEOUT = 180
PULL_TIMEOUT = 60


def repo_slug(repo: str) -> str:
    """owner/name -> owner__name, so it is one directory level."""
    return repo.replace("/", "__")


def run(cmd: list[str], cwd: str | None = None, timeout: int = 60) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    except FileNotFoundError as exc:
        return 127, str(exc)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def checkin(repo: str, repos_dir: str) -> dict:
    """Clone or pull one repository and report where it ended up."""
    slug = repo_slug(repo)
    path = os.path.join(repos_dir, slug)
    url = f"https://github.com/{repo}.git"
    record = {"repo": repo, "path": path, "status": "ok", "commit": "", "error": ""}

    if os.path.isdir(os.path.join(path, ".git")):
        code, out = run(
            ["git", "-C", path, "fetch", "--depth", "1", "origin"], timeout=PULL_TIMEOUT
        )
        if code != 0:
            record["status"] = "fetch_failed"
            record["error"] = out[-300:]
            return record
        # The default branch is what the raw URLs follow.
        code, out = run(["git", "-C", path, "rev-parse", "FETCH_HEAD"], timeout=30)
        if code != 0:
            code, out = run(["git", "-C", path, "rev-parse", "HEAD"], timeout=30)
        record["commit"] = out.strip()[:40] if code == 0 else ""
        return record

    os.makedirs(repos_dir, exist_ok=True)
    tmp = f"{path}.tmp"
    code, out = run(
        ["git", "clone", "--depth", "1", "--quiet", url, tmp], timeout=CLONE_TIMEOUT
    )
    if code != 0:
        record["status"] = "clone_failed"
        record["error"] = out[-300:]
        run(["rm", "-rf", tmp], timeout=30)
        return record
    try:
        os.rename(tmp, path)
    except OSError:
        record["status"] = "clone_failed"
        record["error"] = "could not move the clone into place"
        return record

    code, out = run(["git", "-C", path, "rev-parse", "HEAD"], timeout=30)
    record["commit"] = out.strip()[:40] if code == 0 else ""
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", default=os.path.join(REPO_ROOT, "sources/sources.json"))
    parser.add_argument("--work-dir", default=os.path.join(REPO_ROOT, "work"))
    parser.add_argument("--repos-dir", default="", help="default: <work-dir>/repos")
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    with open(args.sources) as fh:
        payload = json.load(fh)
    sources = payload["sources"] if isinstance(payload, dict) else payload

    repos = sorted({s["repo"] for s in sources})
    repos_dir = args.repos_dir or os.path.join(args.work_dir, "repos")
    os.makedirs(repos_dir, exist_ok=True)

    print(f"checking in {len(repos)} repositories into {repos_dir}", flush=True)
    started = time.time()
    records = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for record in pool.map(lambda r: checkin(r, repos_dir), repos):
            records.append(record)
            mark = {"ok": "ok", "clone_failed": "FAIL", "fetch_failed": "FAIL"}.get(
                record["status"], "?"
            )
            print(
                f"  {mark:>4}  {record['repo']:<48} {record['commit'][:10]}",
                flush=True,
            )

    failed = [r for r in records if r["status"] != "ok"]
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "repos_dir": repos_dir,
        "total": len(records),
        "ok": len(records) - len(failed),
        "failed": len(failed),
        "duration_s": round(time.time() - started, 1),
        "repositories": records,
    }
    os.makedirs(args.work_dir, exist_ok=True)
    out_path = os.path.join(args.work_dir, "repos.json")
    with open(out_path, "w") as fh:
        json.dump(report, fh, indent=1)

    print(
        f"\n{report['ok']}/{report['total']} repositories checked in "
        f"in {report['duration_s']}s"
    )
    for record in failed:
        print(f"  failed: {record['repo']}: {record['error'][:120]}")
    print(f"report: {out_path}")
    # A repository that cannot be cloned is not fatal: the raw URL still works
    # for its lists.  Only a total failure is an error.
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
