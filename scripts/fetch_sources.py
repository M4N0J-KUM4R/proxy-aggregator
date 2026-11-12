#!/usr/bin/env python3
"""Download every proxy list in sources/sources.json, normalize the lines and
build the validator config.

Outputs (all under --work-dir, default ./work):
  raw/<source_id>.txt      the untouched downloaded list
  pools/<protocol>_<n>.txt normalized, scheme-prefixed, chunked proxy lists
  provenance.json           proxy -> list of source ids it appeared in
  config.toml              validator config pointing at the local pools
  fetch_report.json        per-source HTTP status, line counts, timings

A source that fails to download is skipped, not fatal: the run continues with
whatever the remaining sources produced. Exit code is non-zero only when no
source at all could be read.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

# A proxy host: an IPv4 literal, a hostname, or a bracketed IPv6 literal.
HOST_RE = re.compile(r"^(\[[0-9A-Fa-f:]+\]|[0-9A-Za-z._-]+)$")
SCHEME_RE = re.compile(r"^(https?|socks4|socks5)://", re.IGNORECASE)
CIDR_RE = re.compile(r"^([0-9]{1,3}(?:\.[0-9]{1,3}){3})/(\d{1,2})$")

# Widest CIDR that gets expanded. A /16 is 65534 hosts and a single such entry
# would blow past the validator's per-source cap, so anything broader than
# /24 is dropped instead of expanded.
MAX_CIDR_PREFIX = 24
MAX_CIDR_HOSTS = 256
MAX_POOL_ENTRIES = 40000

# Total unique proxies handed to the validator. Dead proxies cost the connect
# timeout each, so the wall clock is roughly
# total / max_concurrent_checks * connect_timeout. Overridable per run.
DEFAULT_MAX_TOTAL_UNIQUE = 120000


def ssl_context() -> ssl.SSLContext:
    """A verifying SSL context.

    Some Python installs ship without a usable system CA store, which makes
    every https request fail with CERTIFICATE_VERIFY_FAILED. Prefer certifi
    when it is available, fall back to the system store, and as a last resort
    use certifi's bundle even if the import of the package itself failed.
    """
    context = ssl.create_default_context()
    try:
        import certifi  # noqa: PLC0415 - optional dependency

        context.load_verify_locations(certifi.where())
        return context
    except Exception:  # noqa: BLE001
        pass
    if not context.get_ca_certs():
        raise RuntimeError(
            "no CA certificates available for https downloads; "
            "install certifi (pip install certifi)"
        )
    return context


SSL_CONTEXT = ssl_context()


def fetch(url: str, timeout: int, retries: int = 2) -> tuple[int, bytes, str]:
    """GET a URL, retrying transient failures. Returns (status, body, error)."""
    last_error = ""
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(
                request, timeout=timeout, context=SSL_CONTEXT
            ) as response:
                return response.status, response.read(), ""
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code in (404, 403, 401, 451):
                break
        except Exception as exc:  # noqa: BLE001 - report whatever the socket did
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    return 0, b"", last_error


def expand_cidr(network: str) -> list[str]:
    """Expand a CIDR into its usable host addresses, or nothing if too large."""
    match = CIDR_RE.match(network)
    if not match:
        return []
    try:
        net = ipaddress.ip_network(network, strict=False)
    except ValueError:
        return []
    # net.prefixlen grows as the block gets smaller: a /24 has 256 hosts, a
    # /16 has 65534. Only blocks at least as narrow as MAX_CIDR_PREFIX are
    # expanded, and the host count is capped as a second guard.
    if net.prefixlen < MAX_CIDR_PREFIX:
        return []
    hosts = list(net.hosts())
    if len(hosts) > MAX_CIDR_HOSTS:
        return []
    return hosts


def parse_line(line: str) -> tuple[str, str, int, str | None] | None:
    """Parse one list line into (protocol, host, port, credentials).

    Handles the formats the upstream lists actually use:
      host:port
      scheme://host:port
      user:pass@host:port
      host:port:Country              (zloi-user/hideip.me)
      host:port | PROTO | CC | Anon  (anutmagang/Free-HighQuality-Proxy-Socks)
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # Pipe-delimited rows carry the protocol in a later column.
    piped_protocol = None
    if "|" in line:
        columns = [column.strip() for column in line.split("|")]
        first = columns[0]
        for column in columns[1:]:
            if column.upper() in ("HTTP", "HTTPS", "SOCKS4", "SOCKS5"):
                piped_protocol = column.lower()
                break
        line = first

    scheme = None
    match = SCHEME_RE.match(line)
    if match:
        scheme = match.group(1).lower()
        line = line[match.end():]
    elif line.startswith("//"):
        return None

    credentials = None
    if "@" in line:
        credentials, _, line = line.rpartition("@")
        if not credentials:
            return None

    # A trailing "host:port:Country" row keeps the country in a third column.
    host_part, separator, port_part = line.rpartition(":")
    if not separator:
        return None
    if not port_part.isdigit() and ":" in host_part:
        # The last field was not the port, so the row carries a third column.
        host_part, separator, port_part = host_part.rpartition(":")
        if not separator or not port_part.isdigit():
            return None

    if not port_part.isdigit():
        return None
    port = int(port_part)
    if not 0 < port <= 65535:
        return None

    host = host_part.strip()
    if not host:
        return None

    if "/" in host:
        # CIDR source: expanded by the caller, one host per line.
        return None

    if not HOST_RE.match(host):
        return None

    # https:// in a list means an HTTP proxy that also speaks CONNECT, so it is
    # checked as http like every other HTTP proxy.
    protocol = scheme if scheme in ("socks4", "socks5") else "http"
    if piped_protocol in ("socks4", "socks5"):
        protocol = piped_protocol
    return protocol, host, port, credentials


def normalize_source(text: str, declared_protocol: str) -> dict[str, list[str]]:
    """Split one downloaded list into protocol -> list of "host:port" lines."""
    buckets: dict[str, list[str]] = {"http": [], "socks4": [], "socks5": []}
    seen: set[str] = set()

    for raw_line in text.splitlines():
        parsed = parse_line(raw_line)
        if parsed is None:
            # Retry as a CIDR block, which the single-line parser rejects.
            stripped = raw_line.strip()
            match = re.match(
                r"^(?:(https?|socks4|socks5)://)?([0-9./]+):(\d{1,5})$", stripped
            )
            if not match:
                continue
            scheme = (match.group(1) or "").lower()
            hosts = expand_cidr(match.group(2))
            port = int(match.group(3))
            if not hosts or not 0 < port <= 65535:
                continue
            protocol = scheme if scheme in ("socks4", "socks5") else declared_protocol
            if protocol not in ("http", "socks4", "socks5"):
                protocol = declared_protocol
            for host in hosts:
                key = f"{protocol}://{host}:{port}"
                if key in seen:
                    continue
                seen.add(key)
                buckets[protocol].append(f"{host}:{port}")
            continue

        protocol, host, port_number, credentials = parsed
        if declared_protocol in ("socks4", "socks5") and protocol == "http":
            protocol = declared_protocol
        key = f"{protocol}://{host}:{port_number}"
        if key in seen:
            continue
        seen.add(key)
        entry = f"{host}:{port_number}"
        if credentials:
            entry = f"{credentials}@{entry}"
        buckets[protocol].append(entry)
    return buckets


def build_config(pools: dict[str, list[str]], work_dir: str) -> str:
    """Render the validator config that reads the local pools."""
    lines = [
        "#:schema ../config-schema.json",
        "",
        "debug = false",
        "",
        "[output]",
        'path = "./out"',
        "sort_by_speed = true",
        "",
        "[output.txt]",
        "enabled = true",
        "",
        "[output.json]",
        "enabled = true",
        "include_asn = true",
        "include_geolocation = true",
        "",
        "[checking]",
        'check_url = "https://ipv4.icanhazip.com"',
        "max_concurrent_checks = 1024",
        "timeout = 10.0",
        "connect_timeout = 5.0",
        f'user_agent = "{USER_AGENT}"',
        "",
        "[scraping]",
        "max_proxies_per_source = 100000",
        "timeout = 15.0",
        "connect_timeout = 5.0",
        'proxy = ""',
        f'user_agent = "{USER_AGENT}"',
        "",
    ]
    for protocol in ("http", "socks4", "socks5"):
        lines.append(f"[scraping.{protocol}]")
        lines.append("enabled = true")
        lines.append("urls = [")
        for pool in pools.get(protocol, []):
            relative = os.path.relpath(pool, work_dir)
            lines.append(f'  "./{relative}",')
        lines.append("]")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sources",
        default=os.path.join(REPO_ROOT, "sources", "sources.json"),
        help="path to the source manifest",
    )
    parser.add_argument("--work-dir", default=os.path.join(REPO_ROOT, "work"))
    parser.add_argument("--timeout", type=int, default=25, help="per-request timeout")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument(
        "--max-proxies",
        type=int,
        default=DEFAULT_MAX_TOTAL_UNIQUE,
        help="total unique proxies to hand to the validator",
    )
    args = parser.parse_args()

    with open(args.sources, encoding="utf-8") as handle:
        manifest = json.load(handle)
    sources = manifest["sources"]

    work_dir = os.path.abspath(args.work_dir)
    raw_dir = os.path.join(work_dir, "raw")
    pool_dir = os.path.join(work_dir, "pools")
    for directory in (raw_dir, pool_dir):
        os.makedirs(directory, exist_ok=True)

    print(f"[fetch] {len(sources)} sources, work dir {work_dir}", flush=True)

    def load(source: dict) -> dict:
        started = time.time()
        status, body, error = fetch(source["url"], args.timeout)
        elapsed = round(time.time() - started, 2)
        record = {
            "id": source["id"],
            "repo": source["repo"],
            "protocol": source["protocol"],
            "url": source["url"],
            "status": status,
            "error": error,
            "seconds": elapsed,
            "bytes": len(body),
            "lines": 0,
            "parsed": 0,
        }
        if status != 200:
            print(f"[fetch] FAIL {source['id']:<24} {error or status}", flush=True)
            return record
        text = body.decode("utf-8", errors="replace")
        record["lines"] = text.count("\n") + (0 if not text else 1)
        with open(os.path.join(raw_dir, f"{source['id']}.txt"), "w", encoding="utf-8") as handle:
            handle.write(text)
        return record

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        results = list(executor.map(load, sources))

    report = []
    merged: dict[str, dict[str, list[str]]] = {
        "http": {}, "socks4": {}, "socks5": {}
    }
    provenance: dict[str, list[str]] = {}

    for source, record in zip(sources, results):
        report.append(record)
        if record["status"] != 200:
            continue
        raw_path = os.path.join(raw_dir, f"{source['id']}.txt")
        with open(raw_path, encoding="utf-8") as handle:
            text = handle.read()
        buckets = normalize_source(text, source["protocol"])
        total = 0
        for protocol, entries in buckets.items():
            if not entries:
                continue
            total += len(entries)
            for entry in entries:
                key = f"{protocol}://{entry}"
                provenance.setdefault(key, []).append(source["id"])
        record["parsed"] = total
        for protocol, entries in buckets.items():
            for entry in entries:
                merged[protocol].setdefault(entry, source["id"])
        print(
            f"[fetch] ok   {source['id']:<24} {record['lines']:>7} lines -> {total:>6} parsed",
            flush=True,
        )

    ok_sources = [record for record in report if record["status"] == 200]
    if not ok_sources:
        print("[fetch] every source failed", file=sys.stderr)
        with open(os.path.join(work_dir, "fetch_report.json"), "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
        return 1

    # Write the deduplicated pools, chunked so no single file trips the
    # validator's per-source limit. When the total exceeds the budget, each
    # protocol is trimmed proportionally so one protocol cannot starve the
    # others.
    budget = max(args.max_proxies, 1000)
    total_unique = sum(len(merged[protocol]) for protocol in ("http", "socks4", "socks5"))
    scale = min(1.0, budget / total_unique) if total_unique else 1.0

    pool_files: dict[str, list[str]] = {"http": [], "socks4": [], "socks5": []}
    grand_total = 0
    trimmed = False
    for protocol in ("http", "socks4", "socks5"):
        entries = sorted(merged[protocol].keys())
        if scale < 1.0:
            keep = max(int(len(entries) * scale), 1)
            entries = entries[:keep]
            trimmed = True
        grand_total += len(entries)
        for index in range(0, len(entries), MAX_POOL_ENTRIES):
            chunk = entries[index:index + MAX_POOL_ENTRIES]
            path = os.path.join(pool_dir, f"{protocol}_{index // MAX_POOL_ENTRIES:03d}.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(f"{protocol}://{entry}" for entry in chunk))
                handle.write("\n")
            pool_files[protocol].append(path)
        print(f"[pool]  {protocol:<7} {len(entries):>7} unique proxies", flush=True)

    if trimmed:
        print(
            f"[pool]  trimmed {total_unique} unique proxies down to the "
            f"{budget} budget",
            file=sys.stderr,
        )

    config = build_config(pool_files, work_dir)
    config_path = os.path.join(work_dir, "config.toml")
    with open(config_path, "w", encoding="utf-8") as handle:
        handle.write(config)

    with open(os.path.join(work_dir, "provenance.json"), "w", encoding="utf-8") as handle:
        json.dump(provenance, handle, indent=1, sort_keys=True)

    with open(os.path.join(work_dir, "fetch_report.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)

    print(
        f"[fetch] done: {len(ok_sources)}/{len(sources)} sources ok, "
        f"{grand_total} unique proxies, config at {config_path}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
