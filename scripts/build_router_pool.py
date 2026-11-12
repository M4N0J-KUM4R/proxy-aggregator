#!/usr/bin/env python3
"""Build the router's proxy pool from the validated set.

The router reads one file: a list of scheme://host:port lines.  The
aggregator produces a validated pool, but the router's own file may hold
proxies the aggregator never tested - a proxy that works against DeepAI even
though it is not in any of the scraped lists, for instance.

So the pool is merged rather than replaced:

  - every proxy the validator confirmed works is in
  - a proxy the validator tested and found dead is dropped
  - a proxy the validator never tested is kept

The dead set is what makes this worth doing: without it the pool would only
ever grow, and a pool of dead proxies is a pool that stalls.

Usage:
  python3 build_router_pool.py \\
      --working output/all.txt \\
      --tested work/pools \\
      --previous /Users/manojkumar/gml/9router/deepai/proxies.txt \\
      --out /Users/manojkumar/gml/9router/deepai/proxies.txt
"""

from __future__ import annotations

import argparse
import os
import sys

SCHEMES = ("http", "socks4", "socks5")


def read_lines(path: str) -> list[str]:
    if not path or not os.path.isfile(path):
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def normalize(line: str) -> str:
    """scheme://host:port, lowercased scheme, no path or credentials kept."""
    line = line.strip()
    if not line:
        return ""
    scheme, sep, rest = line.partition("://")
    if not sep:
        scheme, rest = "http", line
    scheme = scheme.lower()
    if scheme not in SCHEMES:
        return ""
    rest = rest.split("/")[0].strip()
    if not rest:
        return ""
    return f"{scheme}://{rest}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--working", default="output/all.txt",
                        help="the validated pool")
    parser.add_argument("--tested", default="work/pools",
                        help="directory of pool files that were handed to the validator")
    parser.add_argument("--previous", default="",
                        help="the pool the router is using now")
    parser.add_argument("--out", default="",
                        help="where to write the merged pool; stdout when empty")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    working = {n for n in (normalize(l) for l in read_lines(args.working)) if n}

    tested: set[str] = set()
    if os.path.isdir(args.tested):
        for name in sorted(os.listdir(args.tested)):
            if not name.endswith(".txt"):
                continue
            for line in read_lines(os.path.join(args.tested, name)):
                value = normalize(line)
                if value:
                    tested.add(value)

    previous = {n for n in (normalize(l) for l in read_lines(args.previous)) if n}

    # Tested and not working means dead.  Not tested means unknown, and an
    # unknown proxy stays: the router may be using it for a reason the
    # validator cannot see.
    dead = tested - working
    unknown = previous - tested
    merged = (previous - dead) | working

    print(f"working (validated):  {len(working)}")
    print(f"tested by validator:  {len(tested)}")
    print(f"previous pool:        {len(previous)}")
    print(f"dropped (tested dead):{len(previous & dead)}")
    print(f"kept (never tested):  {len(unknown)}")
    print(f"added (newly working):{len(working - previous)}")
    print(f"merged pool:          {len(merged)}")

    if args.dry_run:
        return 0

    lines = sorted(merged)
    body = "\n".join(lines) + ("\n" if lines else "")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(body)
        print(f"written: {args.out}")
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
