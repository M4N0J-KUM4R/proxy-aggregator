#!/usr/bin/env python3
"""Turn the validator's JSON output into categorized proxy lists.

Reads  : work/out/proxies.json   (validator output)
        work/provenance.json      (proxy -> source ids it came from)
Writes : output/all.txt
        output/by-protocol/{http,socks4,socks5}.txt
        output/by-country/<ISO>.txt
        output/by-speed/{fast,medium,slow}.txt
        output/by-network/{datacenter,residential}.txt
        output/by-anonymity/{elite,anonymous,transparent}.txt
        output/proxies.json
        output/summary.json
        output/summary.md

Categories are derived only from what the validator measured plus the source
list each proxy came from. Anonymity comes from the upstream list a proxy was
published in, because the validator measures reachability, not header leakage.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)

# Response time buckets, in seconds.
SPEED_TIERS = (("fast", 1.0), ("medium", 3.0), ("slow", float("inf")))

# Network owners that are known hosting providers. A proxy exiting on one of
# these is a datacenter IP, which is what most buyers want to avoid.
DATACENTER_KEYWORDS = (
    "amazon", "aws", "google", "microsoft", "azure", "digitalocean",
    "hetzner", "ovh", "linode", "akamai", "cloudflare", "vultr", "contabo",
    "oracle", "alibaba", "tencent", "ibm", "leaseweb", "choopa", "m247",
    "datacamp", "scaleway", "hosting", "datacenter", "data center",
    "colocation", "server", "cloud", "vps", "hosting provider",
    "internet exchange", "peering", "backbone", "cdn", "edge network",
)

# Network owners that are access providers, which usually means the exit IP
# belongs to a consumer connection rather than a rented server.
RESIDENTIAL_KEYWORDS = (
    "broadband", "cable", "mobile", "wireless", "cellular", "fiber",
    "fibre", "telecom", "communications", "isp", "internet service",
    "cablevision", "verizon", "at&t", "sprint", "vodafone", "deutsche",
    "orange", "telefonica", "kddi", "ntt", "chinanet", "comcast", "cox",
)

# Source id fragments that mark the list a proxy was published in. The
# upstream repos publish separately tagged lists, and the tag is what carries
# the anonymity class: the validator measures reachability, not header leakage.
ELITE_SOURCE_HINTS = ("-elite", "elite-")
ANONYMOUS_SOURCE_HINTS = ("-anon", "anon-", "anonymous")
TRANSPARENT_SOURCE_HINTS = ("-transp", "transp-", "transparent")


def load_json(path: str):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def speed_tier(timeout: float | None) -> str:
    if timeout is None:
        return "unknown"
    for name, ceiling in SPEED_TIERS:
        if timeout < ceiling:
            return name
    return "slow"


def network_type(proxy: dict) -> str:
    """datacenter, residential or unknown, from the ASN owner of the exit IP."""
    asn = proxy.get("asn") or {}
    organization = (asn.get("autonomous_system_organization") or "").lower()
    if not organization:
        return "unknown"
    for keyword in DATACENTER_KEYWORDS:
        if keyword in organization:
            return "datacenter"
    for keyword in RESIDENTIAL_KEYWORDS:
        if keyword in organization:
            return "residential"
    return "unknown"


def anonymity_class(source_ids: list[str]) -> str:
    """Anonymity as published by the upstream list the proxy came from.

    The most specific tag wins: a proxy published in both an elite list and an
    anonymous list is elite, and one that also shows up in a transparent list
    is transparent, because that list says the proxy leaks the client IP.
    """
    lowered = [source_id.lower() for source_id in source_ids]
    if any(
        hint in source_id for source_id in lowered for hint in TRANSPARENT_SOURCE_HINTS
    ):
        return "transparent"
    if any(hint in source_id for source_id in lowered for hint in ELITE_SOURCE_HINTS):
        return "elite"
    if any(hint in source_id for source_id in lowered for hint in ANONYMOUS_SOURCE_HINTS):
        return "anonymous"
    return "unknown"


def country_of(proxy: dict) -> str:
    geo = proxy.get("geolocation") or {}
    country = geo.get("country") or {}
    return country.get("iso_code") or "??"


def write_list(path: str, lines: list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", default=os.path.join(REPO_ROOT, "work"))
    parser.add_argument("--output-dir", default=os.path.join(REPO_ROOT, "output"))
    args = parser.parse_args()

    work_dir = os.path.abspath(args.work_dir)
    output_dir = os.path.abspath(args.output_dir)

    proxies = load_json(os.path.join(work_dir, "out", "proxies.json"))
    if proxies is None:
        print("[cat] no validator output found", file=sys.stderr)
        return 1
    provenance = load_json(os.path.join(work_dir, "provenance.json")) or {}

    # Drop stale files so a failed run leaves no outdated lists behind.
    for name in ("by-protocol", "by-country", "by-speed", "by-network", "by-anonymity"):
        directory = os.path.join(output_dir, name)
        if os.path.isdir(directory):
            for entry in os.listdir(directory):
                path = os.path.join(directory, entry)
                if os.path.isfile(path):
                    os.remove(path)

    by_protocol: dict[str, list[str]] = defaultdict(list)
    by_country: dict[str, list[str]] = defaultdict(list)
    by_speed: dict[str, list[str]] = defaultdict(list)
    by_network: dict[str, list[str]] = defaultdict(list)
    by_anonymity: dict[str, list[str]] = defaultdict(list)

    records = []
    all_lines = []

    for proxy in proxies:
        protocol = proxy.get("protocol") or "unknown"
        host = proxy.get("host")
        port = proxy.get("port")
        if not host or not port:
            continue

        credentials = ""
        if proxy.get("username"):
            credentials = f"{proxy['username']}:{proxy.get('password') or ''}@"
        line = f"{credentials}{host}:{port}"
        prefixed = f"{protocol}://{line}"

        tier = speed_tier(proxy.get("timeout"))
        country = country_of(proxy)
        net = network_type(proxy)
        source_ids = provenance.get(prefixed) or provenance.get(line) or []
        anonymity = anonymity_class(source_ids)

        by_protocol[protocol].append(prefixed)
        by_country[country].append(prefixed)
        by_speed[tier].append(prefixed)
        by_network[net].append(prefixed)
        by_anonymity[anonymity].append(prefixed)
        all_lines.append(prefixed)

        geo = proxy.get("geolocation") or {}
        country_name = ((geo.get("country") or {}).get("names") or {}).get("en", "")
        asn = proxy.get("asn") or {}
        records.append(
            {
                "proxy": prefixed,
                "protocol": protocol,
                "host": host,
                "port": port,
                "timeout": proxy.get("timeout"),
                "speed": tier,
                "country": country,
                "country_name": country_name,
                "city": ((geo.get("city") or {}).get("names") or {}).get("en", ""),
                "exit_ip": proxy.get("exit_ip"),
                "asn_number": asn.get("autonomous_system_number"),
                "asn_org": asn.get("autonomous_system_organization"),
                "network": net,
                "anonymity": anonymity,
                "sources": source_ids,
            }
        )

    all_lines.sort()
    write_list(os.path.join(output_dir, "all.txt"), all_lines)

    for name, mapping in (
        ("by-protocol", by_protocol),
        ("by-country", by_country),
        ("by-speed", by_speed),
        ("by-network", by_network),
        ("by-anonymity", by_anonymity),
    ):
        for key, lines in mapping.items():
            write_list(os.path.join(output_dir, name, f"{key}.txt"), sorted(lines))

    with open(os.path.join(output_dir, "proxies.json"), "w", encoding="utf-8") as handle:
        json.dump(records, handle, indent=2)

    protocol_counts = Counter(record["protocol"] for record in records)
    speed_counts = Counter(record["speed"] for record in records)
    network_counts = Counter(record["network"] for record in records)
    anonymity_counts = Counter(record["anonymity"] for record in records)
    country_counts = Counter(record["country"] for record in records)

    summary = {
        "total_working": len(records),
        "by_protocol": dict(protocol_counts.most_common()),
        "by_speed": dict(speed_counts.most_common()),
        "by_network": dict(network_counts.most_common()),
        "by_anonymity": dict(anonymity_counts.most_common()),
        "countries": len(country_counts),
        "top_countries": dict(country_counts.most_common(25)),
    }
    with open(os.path.join(output_dir, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)

    markdown = [
        "# Proxy pool summary",
        "",
        f"Working proxies: **{len(records)}**",
        "",
        "## By protocol",
        "",
        "| Protocol | Count |",
        "| --- | ---: |",
    ]
    markdown += [f"| {key} | {value} |" for key, value in protocol_counts.most_common()]
    markdown += [
        "",
        "## By speed",
        "",
        "| Tier | Count |",
        "| --- | ---: |",
    ]
    markdown += [f"| {key} | {value} |" for key, value in speed_counts.most_common()]
    markdown += [
        "",
        "## By network type",
        "",
        "| Type | Count |",
        "| --- | ---: |",
    ]
    markdown += [f"| {key} | {value} |" for key, value in network_counts.most_common()]
    markdown += [
        "",
        "## By anonymity",
        "",
        "| Class | Count |",
        "| --- | ---: |",
    ]
    markdown += [f"| {key} | {value} |" for key, value in anonymity_counts.most_common()]
    markdown += [
        "",
        f"Countries represented: **{len(country_counts)}**",
        "",
        "## Top countries",
        "",
        "| Country | Count |",
        "| --- | ---: |",
    ]
    markdown += [f"| {key} | {value} |" for key, value in country_counts.most_common(25)]
    markdown.append("")
    with open(os.path.join(output_dir, "summary.md"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(markdown))

    print(
        f"[cat] {len(records)} working proxies -> {output_dir} "
        f"({len(country_counts)} countries)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
