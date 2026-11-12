# Free proxy pool

Aggregates free proxy lists from GitHub, validates every entry with
[proxy-scraper-checker](https://github.com/monosans/proxy-scraper-checker),
keeps only the ones that actually work, and sorts the survivors into
categories.

A GitHub Actions workflow runs the whole pipeline on a schedule and commits
the result back to this repository.

## What it does

1. **Check in** — `scripts/checkin_sources.py` clones each of the 34 upstream
   proxy-list repositories shallow into `work/repos/`, or pulls the ones
   already there, and records the commit each one is at.
2. **Fetch** — `scripts/fetch_sources.py` reads the lists
   in `sources/sources.json` (105 lists across 34 repositories), taking them
   from the checkouts where the file is there and from the raw URL where it
   is not, normalizes the line formats, deduplicates and writes per-protocol
   pools.
3. **Validate** — the validator fetches a real URL through every proxy. A
   proxy only survives if it delivers the complete response in time, so dead
   and stalling entries are dropped.
4. **Categorize** — `scripts/categorize.py` sorts the survivors by protocol,
   country, speed, network type and anonymity class.
5. **Merge** — `scripts/build_router_pool.py` folds the validated pool into
   the router's own pool: a proxy the validator tested and found dead is
   dropped, one it never tested is kept, and the newly working ones are
   added.

## The local refresh pipeline

The GitHub Actions workflow refreshes this repository's own `output/`. The
local pipeline does more: it publishes the result into the **9router**
repository, where the router reads it from, and reloads the running shim.

```bash
./scripts/install_schedule.sh install 6 41   # 06:41 daily, launchd
./scripts/install_schedule.sh status
./scripts/install_schedule.sh run           # run it now, in the foreground
```

`scripts/refresh_and_publish.sh` is the whole thing:

```
check in the 34 repositories
  -> scrape the 105 lists
  -> validate every proxy in them
  -> categorize the survivors
  -> merge into the router's pool
  -> publish into the 9router repository, commit, push
  -> reload the running shim
```

A step that fails stops the run before the publish, so a broken pipeline never
replaces a good pool. The log is `work/refresh.log`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGG_DIR` | the checkout | where the pipeline runs |
| `ROUTER_REPO` | `~/gml/9router-repo` | the repository the result is published into |
| `LIVE_ROUTER_DIR` | `~/gml/9router` | the running stack, whose pool file is refreshed |
| `SHIM_URL` | `http://127.0.0.1:8802` | the running shim, for the reload |
| `MAX_PROXIES` | 120000 | how many unique proxies to validate per run |
| `VALIDATOR` | `validator/proxy-scraper-checker` | the validator binary |

The validator binary is not in this repository. `refresh_and_publish.sh`
looks for it in `validator/`, and copies it out of `~/Downloads` when it is
not there.

## The shim reload

The router reads its pool from one file, and the shim used to read it once at
startup — so a refreshed pool only took effect on the next restart, and a
restart throws away every warm session. `POST /reload` on the shim re-reads
the file and rebinds only the accounts whose proxy is no longer in it. The
accounts keep their sessions.

```bash
curl -X POST http://127.0.0.1:8802/reload
# {"proxies":921,"rebound":96}
```

## Output layout

```
output/
├── all.txt              every working proxy, scheme-prefixed
├── proxies.json         full records: speed, country, ASN, anonymity, sources
├── summary.json         machine-readable counts
├── summary.md           human-readable counts
├── by-protocol/         http.txt, socks4.txt, socks5.txt
├── by-country/          one file per ISO country code
├── by-speed/            fast.txt, medium.txt, slow.txt
├── by-network/          datacenter.txt, residential.txt, unknown.txt
└── by-anonymity/        elite.txt, anonymous.txt, transparent.txt, unknown.txt
```

Every line is `scheme://host:port`, ready to paste into a proxy client.

## Categories

| Category | Derived from |
| --- | --- |
| protocol | the list a proxy came from, or its own scheme |
| country | GeoLite2 city database, looked up by exit IP |
| speed | measured response time: fast < 1s, medium < 3s, slow above |
| network | the ASN owner of the exit IP, matched against hosting and ISP names |
| anonymity | the upstream list a proxy was published in |

The network and anonymity categories are heuristics, not measurements. A
datacenter exit is an IP registered to a hosting provider; anonymity is taken
from the tagged list the proxy was published in, because the validator checks
reachability and does not inspect leaked headers.

## Running it locally

```bash
pip install certifi
python3 scripts/fetch_sources.py --work-dir work --max-proxies 120000
PROXY_SCRAPER_CHECKER_CONFIG=./work/config.toml ./proxy-scraper-checker
python3 scripts/categorize.py --work-dir work --output-dir output
```

The validator binary is not in this repository. Download it for your platform
from the upstream releases page. It reads `config.toml` from the current
directory, or from the path in `PROXY_SCRAPER_CHECKER_CONFIG`.

## The workflow

`.github/workflows/update-proxies.yml` runs every three hours and on manual
dispatch. It installs the validator from a pinned upstream release, verifies
the archive checksum, runs the pipeline, and commits the refreshed `output/`
directory.

| Input | Default | Purpose |
| --- | --- | --- |
| `max_proxies` | 120000 | how many unique proxies to validate per run |
| `timeout_minutes` | 45 | job timeout |

The proxy budget bounds the wall clock: dead proxies cost the connect timeout
each, so the run takes roughly `total / max_concurrent_checks * connect_timeout`.

The workflow needs `contents: write` to push results, and caches the GeoLite
databases so each run does not re-download them.

## Sources

`sources/sources.json` lists every source with its repository and the exact
path inside it. Each entry was verified to return HTTP 200 when it was added.
The fetch script reports the status of every source on each run, so a list
that disappears upstream shows up in `work/fetch_report.json` rather than
silently shrinking the pool.

To add a source, append an entry with a unique `id`, the `repo` it belongs to,
the `protocol` the list holds (`http`, `socks4`, `socks5` or `mixed`) and the
raw URL.

## Notes

Free proxies are unreliable by nature and many are monitored or malicious. Use
them for testing traffic, not for anything that carries credentials.
