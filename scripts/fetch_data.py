#!/usr/bin/env python3
"""
ETH Unstaking Queue Data Fetcher
--------------------------------
Owns its own queue history instead of mirroring a third-party git repo.

Why: the previous version read historical_data.json from
etheralpha/validatorqueue-com, which froze on 2026-10-25. Because ETH prices
kept updating from CoinGecko, data/ changed every day, a commit was made every
day, and meta.lastUpdated looked fresh -- while the queue numbers had been
frozen for three weeks. This version:

  1. Reads the current queue snapshot straight from beaconcha.in and appends it
     to data/queue_history.json (one record per UTC day, first reading wins).
  2. Records meta.dataThrough / meta.queueStaleDays so "last run" can never be
     mistaken for "last fresh data" again.
  3. Keeps previously-known prices, so a CoinGecko outage no longer deletes
     recent price history.
  4. Signals staleness to the workflow via $GITHUB_OUTPUT so the Action turns
     red (and emails you) instead of failing silently.

Env:
  BEACONCHAIN_TOKEN  beaconcha.in API key (free: https://beaconcha.in/user/settings)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------- config

BEACONCHAIN_TOKEN = os.environ.get("BEACONCHAIN_TOKEN", "").strip()
QUEUE_ENDPOINT = "https://beaconcha.in/api/v1/validators/queue"

# CoinGecko: Demo (free) and Pro keys use different hosts AND different headers,
# so the tier decides both. Keyless still works, just with tighter rate limits.
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "").strip()
COINGECKO_PRO_API_KEY = os.environ.get("COINGECKO_PRO_API_KEY", "").strip()
COINGECKO_DEMO_BASE = "https://api.coingecko.com/api/v3"
COINGECKO_PRO_BASE = "https://pro-api.coingecko.com/api/v3"
COINGECKO_PATH = "/coins/ethereum/market_chart"


def coingecko_auth() -> tuple[str, dict, str]:
    """Return (url, headers, tier_label) for the key we actually have."""
    if COINGECKO_PRO_API_KEY:
        return (COINGECKO_PRO_BASE + COINGECKO_PATH,
                {"x-cg-pro-api-key": COINGECKO_PRO_API_KEY},
                "pro")
    if COINGECKO_API_KEY:
        return (COINGECKO_DEMO_BASE + COINGECKO_PATH,
                {"x-cg-demo-api-key": COINGECKO_API_KEY},
                "demo")
    return (COINGECKO_DEMO_BASE + COINGECKO_PATH, {}, "keyless")

DATA_DIR = Path(__file__).parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "eth_unstaking_data.json"
QUEUE_HISTORY_FILE = DATA_DIR / "queue_history.json"
HISTORICAL_PRICES_CSV = DATA_DIR / "eth_historical_prices.csv"

# Churn math, kept identical to validatorqueue.com so the appended records stay
# consistent with the seeded history. Post-Pectra the protocol churn limit is
# dynamic, but 256 ETH/epoch is the value the seeded history was built with.
CHURN_ETH_PER_EPOCH = 256
SLOT_SECONDS = 12
SLOTS_PER_EPOCH = 32
EPOCH_SECONDS = SLOT_SECONDS * SLOTS_PER_EPOCH          # 384
DAILY_CHURN_ETH = (86400 / EPOCH_SECONDS) * CHURN_ETH_PER_EPOCH   # 57,600

# Queue data older than this many days is treated as a failure, not a hiccup.
STALE_AFTER_DAYS = 3

GWEI = 1_000_000_000


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now_utc_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def wait_days(queue_eth: float) -> float:
    """Days to clear a queue of `queue_eth` at the current churn rate."""
    if queue_eth <= 0:
        return 0.0
    return round(queue_eth / DAILY_CHURN_ETH, 2)


def set_action_output(key: str, value: str) -> None:
    """Expose a value to later GitHub Actions steps."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")
    except OSError as e:
        print(f"  ! could not write GITHUB_OUTPUT: {e}")


# ---------------------------------------------------------------- queue history


def seed_history_from_output() -> list:
    """Last-resort seed: rebuild history from the published dashboard JSON.

    Only used if data/queue_history.json is missing. The wait-time fields were
    never populated in that file, so prefer shipping the real seed file.
    """
    if not OUTPUT_FILE.exists():
        print("  ! no previous output either -- starting empty")
        return []
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ! could not seed from output: {e}")
        return []

    records = []
    for d in prev.get("data", []):
        if not isinstance(d, dict) or not d.get("date"):
            continue
        records.append({
            "date": d["date"],
            "validators": d.get("validators", 0),
            "entry_queue": d.get("entryQueue", 0),
            "entry_wait": d.get("entryWaitDays", 0),
            "exit_queue": d.get("exitQueue", 0),
            "exit_wait": d.get("exitWaitDays", 0),
            "source": "seeded from eth_unstaking_data.json",
        })
    records.sort(key=lambda r: r["date"])
    print(f"  + seeded {len(records)} records from previous output")
    return records


def load_queue_history() -> list:
    """Load our own queue history. This file is the source of truth."""
    print("Loading queue history...")
    if not QUEUE_HISTORY_FILE.exists():
        print(f"  ! {QUEUE_HISTORY_FILE.name} not found -- seeding from {OUTPUT_FILE.name}")
        return seed_history_from_output()

    try:
        with open(QUEUE_HISTORY_FILE, "r", encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  x could not read queue history: {e}")
        # Refuse to continue rather than silently overwrite real history.
        raise

    records = blob.get("records", []) if isinstance(blob, dict) else blob
    records = [r for r in records if isinstance(r, dict) and r.get("date")]
    records.sort(key=lambda r: r["date"])
    if records:
        print(f"  + {len(records)} records, {records[0]['date']} ~ {records[-1]['date']}")
    return records


def save_queue_history(records: list) -> None:
    blob = {
        "meta": {
            "description": (
                "Self-owned ETH validator queue history. Seeded from "
                "etheralpha/validatorqueue-com (frozen 2026-10-25), appended daily thereafter."
            ),
            "seededFrom": "etheralpha/validatorqueue-com@historical_data.json",
            "seedThrough": "2026-10-25",
            "recordCount": len(records),
            "lastAppended": now_utc_label(),
        },
        "records": records,
    }
    with open(QUEUE_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(blob, f, indent=1, ensure_ascii=False)
    print(f"  + saved {len(records)} records to {QUEUE_HISTORY_FILE.name}")


def fetch_current_queue() -> dict | None:
    """Fetch today's queue snapshot from beaconcha.in."""
    print("Fetching current queue from beaconcha.in...")
    if not BEACONCHAIN_TOKEN:
        print("  x BEACONCHAIN_TOKEN is not set -- cannot fetch queue data")
        print("    Get a free key at https://beaconcha.in/user/settings and add it")
        print("    as a repository secret named BEACONCHAIN_TOKEN.")
        return None

    params = {"apikey": BEACONCHAIN_TOKEN}
    headers = {"apikey": BEACONCHAIN_TOKEN, "Accept": "application/json"}

    for attempt in range(1, 4):
        try:
            r = requests.get(QUEUE_ENDPOINT, params=params, headers=headers, timeout=30)
            print(f"  attempt {attempt}/3 -- HTTP {r.status_code}")

            if r.status_code == 429:
                wait = 30 * attempt
                print(f"  rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue

            r.raise_for_status()
            payload = r.json()
            data = payload.get("data") or {}
            if not data:
                print(f"  x unexpected response shape, keys: {list(payload)[:10]}")
                return None

            # beaconcha.in v1 reports queue sizes in gwei post-Pectra.
            entering_gwei = data.get("beaconchain_entering_balance")
            exiting_gwei = data.get("beaconchain_exiting_balance")

            if entering_gwei is None or exiting_gwei is None:
                print(f"  x balance fields missing, got keys: {list(data)}")
                return None

            entry_eth = round(float(entering_gwei) / GWEI)
            exit_eth = round(float(exiting_gwei) / GWEI)
            validators = data.get("validatorscount") or 0

            record = {
                "date": today_utc(),
                "validators": validators,
                "entry_queue": entry_eth,
                "entry_wait": wait_days(entry_eth),
                "exit_queue": exit_eth,
                "exit_wait": wait_days(exit_eth),
                "current_entry_churn": CHURN_ETH_PER_EPOCH,
                "current_exit_churn": CHURN_ETH_PER_EPOCH,
                "source": "beaconcha.in/api/v1/validators/queue",
            }
            print(f"  + entry {entry_eth:,} ETH / exit {exit_eth:,} ETH / "
                  f"validators {validators:,}")
            return record

        except Exception as e:
            print(f"  attempt {attempt}/3 failed: {e}")
            if attempt < 3:
                print("  waiting 15s before retry...")
                time.sleep(15)

    print("  x all attempts failed")
    return None


def append_today(records: list, record: dict | None) -> tuple[list, bool]:
    """Append today's record. First reading of each UTC day wins."""
    if record is None:
        return records, False

    existing = {r["date"] for r in records}
    if record["date"] in existing:
        print(f"  = {record['date']} already recorded, leaving it as-is")
        return records, False

    records = records + [record]
    records.sort(key=lambda r: r["date"])
    print(f"  + appended {record['date']}")
    return records, True


# ---------------------------------------------------------------- prices


def load_known_prices() -> dict:
    """Reuse prices we already published, so an API outage cannot delete history."""
    print("Loading previously known prices...")
    if not OUTPUT_FILE.exists():
        print("  (no previous output file)")
        return {}
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            prev = json.load(f)
        prices = {
            p["date"]: p["price"]
            for p in prev.get("ethPrices", [])
            if isinstance(p, dict) and p.get("date") and p.get("price")
        }
        print(f"  + {len(prices)} prices carried forward")
        return prices
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ! could not reuse previous prices: {e}")
        return {}


def load_historical_prices() -> dict:
    """Load ETH prices from CSV (covers dates beyond CoinGecko's 365-day window)."""
    print("Loading historical prices from CSV...")
    prices = {}

    if not HISTORICAL_PRICES_CSV.exists():
        print("  ! historical CSV not found, skipping")
        return prices

    try:
        with open(HISTORICAL_PRICES_CSV, "r", encoding="utf-8") as f:
            lines = f.readlines()

        for line in lines[1:]:  # skip header
            line = line.strip().replace("\r", "")
            if not line:
                continue
            parts = line.split("\t")  # Date, Price, Open, High, Low, Vol., Change %
            if len(parts) < 2:
                continue
            try:
                date_key = datetime.strptime(parts[0].strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
                price = round(float(parts[1].strip().strip('"').replace(",", "")), 2)
                prices[date_key] = price
            except (ValueError, IndexError):
                continue

        if prices:
            dates = sorted(prices)
            print(f"  + {len(prices)} records, {dates[0]} ~ {dates[-1]}")
    except OSError as e:
        print(f"  x error loading CSV: {e}")

    return prices


def fetch_eth_prices(days: int = 365) -> dict:
    """Fetch ETH prices from CoinGecko (max 365 days on the free tier)."""
    url, headers, tier = coingecko_auth()
    print(f"Fetching ETH prices from CoinGecko (last {days} days, auth={tier})...")
    params = {"vs_currency": "usd", "days": days, "interval": "daily"}

    for attempt in range(1, 4):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            print(f"  attempt {attempt}/3 -- HTTP {r.status_code}")

            if r.status_code == 429:
                wait = 30 * attempt
                print(f"  rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue

            # A key of the wrong tier (or an expired one) is rejected here.
            # Prices matter more than auth, so drop the key and carry on.
            if r.status_code in (400, 401, 403) and headers:
                print(f"  ! {tier} key rejected (HTTP {r.status_code}) -- "
                      f"retrying keyless")
                print("::warning title=CoinGecko key rejected::"
                      f"COINGECKO_API_KEY was refused with HTTP {r.status_code}. "
                      "Check whether it is a Demo or Pro key.")
                url, headers, tier = COINGECKO_DEMO_BASE + COINGECKO_PATH, {}, "keyless"
                continue

            r.raise_for_status()
            payload = r.json()

            prices = {}
            for ts_ms, price in payload.get("prices", []):
                # UTC, so the day bucket matches the queue records.
                date = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                prices[date] = round(price, 2)

            print(f"  + {len(prices)} price records")
            if prices:
                latest = max(prices)
                print(f"  latest: {latest} = ${prices[latest]:,.2f}")
            return prices

        except Exception as e:
            print(f"  attempt {attempt}/3 failed: {e}")
            if attempt < 3:
                print("  waiting 15s before retry...")
                time.sleep(15)

    print("  x all attempts failed -- keeping previously known prices")
    return {}


# ---------------------------------------------------------------- merge & stats


def merge_data(queue_records: list, prices: dict) -> list:
    """Join queue history with prices."""
    print("Merging queue and price data...")
    merged = []
    matched = 0

    for rec in queue_records:
        date = rec.get("date", "")
        if isinstance(date, (int, float)):
            date = datetime.fromtimestamp(date, tz=timezone.utc).strftime("%Y-%m-%d")
        if isinstance(date, str) and "T" in date:
            date = date.split("T")[0]
        if not date:
            continue

        # `or 0` would turn a real 0 into 0 anyway, but an explicit chain keeps
        # a genuine zero queue distinct from a missing field.
        def pick(*keys):
            for k in keys:
                v = rec.get(k)
                if v is not None:
                    return v
            return 0

        exit_queue = pick("exit_queue_eth", "exitQueue", "exit_queue", "exit_balance")
        entry_queue = pick("entry_queue_eth", "entryQueue", "entry_queue", "entry_balance")
        # The old script looked for exit_wait_days/exitWaitDays only, so the
        # upstream field (exit_wait) was dropped and exitWaitDays was always 0.
        exit_wait = pick("exit_wait_days", "exitWaitDays", "exit_wait")
        entry_wait = pick("entry_wait_days", "entryWaitDays", "entry_wait")

        price = prices.get(date, 0)
        if price > 0:
            matched += 1

        merged.append({
            "date": date,
            "exitQueue": exit_queue,
            "entryQueue": entry_queue,
            "exitWaitDays": exit_wait,
            "entryWaitDays": entry_wait,
            "validators": rec.get("validators", 0),
            "ethPrice": price,
            "exitQueueUSD": round((exit_queue * price) / 1_000_000, 2) if price else 0,
        })

    merged.sort(key=lambda d: d["date"])
    print(f"  + merged {len(merged)} records ({matched} with a price)")
    return merged


def calculate_stats(data: list, prices: dict) -> dict:
    """Summary stats. `current*` always comes from the newest record."""
    if not data:
        return {}

    latest = data[-1]
    exit_nonzero = [d["exitQueue"] for d in data if d["exitQueue"] > 0]

    # Correlation between exit queue size and ETH price.
    pairs = [(d["exitQueue"], d["ethPrice"]) for d in data
             if d["exitQueue"] > 0 and d["ethPrice"] > 0]
    correlation = 0.0
    if len(pairs) > 1:
        qs = [p[0] for p in pairs]
        ps = [p[1] for p in pairs]
        n = len(pairs)
        mq, mp = sum(qs) / n, sum(ps) / n
        num = sum((qs[i] - mq) * (ps[i] - mp) for i in range(n))
        dq = sum((q - mq) ** 2 for q in qs) ** 0.5
        dp = sum((p - mp) ** 2 for p in ps) ** 0.5
        if dq > 0 and dp > 0:
            correlation = round(num / (dq * dp), 4)

    # The newest price we know about, not the price on the newest queue day --
    # those drift apart the moment queue data stalls.
    current_price = prices[max(prices)] if prices else 0

    return {
        # Previously these used the last *non-zero* value, so a genuine
        # zero-queue day silently displayed an older number instead.
        "currentQueue": latest["exitQueue"],
        "currentEntryQueue": latest["entryQueue"],
        "currentNetFlow": round(latest["entryQueue"] - latest["exitQueue"], 2),
        "currentExitWaitDays": latest.get("exitWaitDays", 0),
        "currentEntryWaitDays": latest.get("entryWaitDays", 0),
        "avgQueue": round(sum(exit_nonzero) / len(exit_nonzero), 0) if exit_nonzero else 0,
        "maxQueue": max(exit_nonzero) if exit_nonzero else 0,
        "minQueue": min(exit_nonzero) if exit_nonzero else 0,
        "currentPrice": current_price,
        "correlation": correlation,
        "lastUpdated": now_utc_label(),
    }


def calculate_inflection_points(data: list, threshold: float = 0.5) -> list:
    """Detect local peaks/troughs in the exit queue."""
    points = []
    for i in range(1, len(data) - 1):
        prev = data[i - 1].get("exitQueue", 0)
        curr = data[i].get("exitQueue", 0)
        nxt = data[i + 1].get("exitQueue", 0)
        if prev <= 0 or curr <= 0:
            continue
        change_prev = (curr - prev) / prev
        change_next = (nxt - curr) / curr
        if change_prev > threshold and change_next < 0:
            kind = "peak"
        elif change_prev < -0.3 and change_next > 0.1:
            kind = "trough"
        else:
            continue
        points.append({
            "date": data[i]["date"],
            "type": kind,
            "exitQueue": curr,
            "ethPrice": data[i].get("ethPrice", 0),
        })
    return points


def days_since(date_str: str) -> int:
    try:
        then = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return 9999
    return max(0, (datetime.now(timezone.utc) - then).days)


# ---------------------------------------------------------------- main


def main() -> int:
    print("=" * 62)
    print("ETH Unstaking Queue Data Fetcher")
    print(f"Time: {now_utc_label()}")
    print("=" * 62)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Our own queue history + today's snapshot.
    history = load_queue_history()
    snapshot = fetch_current_queue()
    history, appended = append_today(history, snapshot)
    if appended:
        save_queue_history(history)

    if not history:
        print("\nx no queue history at all -- nothing to build")
        return 1

    # 2. Prices: previously known <- CSV <- CoinGecko (later wins).
    print()
    prices = load_known_prices()
    prices.update(load_historical_prices())
    print("\nWaiting 5s before CoinGecko call...")
    time.sleep(5)
    fresh_prices = fetch_eth_prices(days=365)
    prices.update(fresh_prices)

    price_list = sorted(
        ({"date": d, "price": p} for d, p in prices.items()),
        key=lambda x: x["date"],
    )
    if price_list:
        print(f"\n  + {len(price_list)} total prices, "
              f"{price_list[0]['date']} ~ {price_list[-1]['date']}")

    # 3. Build the dashboard payload.
    print()
    merged = merge_data(history, prices)
    if not merged:
        print("x no data to save")
        return 1

    stats = calculate_stats(merged, prices)
    data_through = merged[-1]["date"]
    stale_days = days_since(data_through)
    is_stale = stale_days > STALE_AFTER_DAYS

    output = {
        "meta": {
            "lastUpdated": stats.get("lastUpdated", ""),
            # The honest field: when the queue data itself actually ends.
            "dataThrough": data_through,
            "queueStaleDays": stale_days,
            "queueStale": is_stale,
            "staleAfterDays": STALE_AFTER_DAYS,
            "dataSource": "beaconcha.in + CoinGecko + Historical CSV",
            "queueSource": "beaconcha.in/api/v1/validators/queue (self-owned history)",
            "recordCount": len(merged),
            "priceRecords": len(price_list),
            "recordsWithPrice": sum(1 for d in merged if d["ethPrice"] > 0),
            "priceThrough": price_list[-1]["date"] if price_list else "",
        },
        "stats": stats,
        "inflectionPoints": calculate_inflection_points(merged)[-20:],
        "ethPrices": price_list,
        "data": merged,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # 4. Report.
    print("\n" + "=" * 62)
    print("SUMMARY")
    print("=" * 62)
    print(f"+ saved {OUTPUT_FILE}")
    print(f"  queue records : {len(merged)}")
    print(f"  price records : {len(price_list)}")
    print(f"  data through  : {data_through}  ({stale_days} day(s) old)")
    print(f"  exit queue    : {stats.get('currentQueue', 0):,} ETH")
    print(f"  entry queue   : {stats.get('currentEntryQueue', 0):,} ETH")
    print(f"  ETH price     : ${stats.get('currentPrice', 0):,.2f}")

    set_action_output("data_through", data_through)
    set_action_output("stale_days", str(stale_days))
    set_action_output("queue_stale", "true" if is_stale else "false")

    if is_stale:
        # Annotated as an error so it is visible in the run summary; the job is
        # failed by a later step, after the data has been committed, so the
        # dashboard can still render its own staleness warning.
        print(f"::error title=Queue data is stale::No new queue data since "
              f"{data_through} ({stale_days} days). The beaconcha.in fetch is "
              f"failing or BEACONCHAIN_TOKEN is invalid.")
    elif snapshot is None:
        print("::warning title=Queue fetch failed::Today's snapshot could not be "
              "fetched, but existing data is still fresh enough.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
