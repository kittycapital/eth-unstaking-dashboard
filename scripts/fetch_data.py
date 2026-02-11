#!/usr/bin/env python3
"""
ETH Unstaking Queue Data Fetcher
Fetches historical validator queue data and ETH prices
Runs daily via GitHub Actions
"""

import json
import requests
from datetime import datetime
from pathlib import Path
import time

# API endpoints
VALIDATOR_QUEUE_URL = "https://raw.githubusercontent.com/etheralpha/validatorqueue-com/main/historical_data.json"

# CoinGecko API - FREE, no API key needed (max 365 days)
COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/ethereum/market_chart"

# Output paths
DATA_DIR = Path(__file__).parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "eth_unstaking_data.json"
HISTORICAL_PRICES_CSV = DATA_DIR / "eth_historical_prices.csv"


def fetch_validator_queue():
    """Fetch historical validator queue data from ValidatorQueue GitHub"""
    print("Fetching validator queue data...")
    try:
        response = requests.get(VALIDATOR_QUEUE_URL, timeout=30)
        response.raise_for_status()
        data = response.json()
        print(f"  ✓ Fetched {len(data)} queue records")
        return data
    except Exception as e:
        print(f"  ✗ Error fetching queue data: {e}")
        return []


def load_historical_prices():
    """Load historical ETH prices from CSV (backfill for dates beyond CoinGecko 365-day limit)"""
    print("Loading historical prices from CSV...")
    prices_map = {}
    
    if not HISTORICAL_PRICES_CSV.exists():
        print("  ⚠ Historical CSV not found, skipping")
        return prices_map
    
    try:
        with open(HISTORICAL_PRICES_CSV, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        # Skip header
        for line in lines[1:]:
            line = line.strip().replace("\r", "")
            if not line:
                continue
            
            # Tab-separated: Date, Price, Open, High, Low, Vol., Change %
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            
            try:
                # Parse date: MM/DD/YYYY -> YYYY-MM-DD
                date_str = parts[0].strip()
                date_obj = datetime.strptime(date_str, "%m/%d/%Y")
                date_key = date_obj.strftime("%Y-%m-%d")
                
                # Parse price: remove quotes and commas
                price_str = parts[1].strip().strip('"').replace(",", "")
                price = round(float(price_str), 2)
                
                prices_map[date_key] = price
            except (ValueError, IndexError):
                continue
        
        print(f"  ✓ Loaded {len(prices_map)} historical price records")
        if prices_map:
            dates = sorted(prices_map.keys())
            print(f"  Range: {dates[0]} ~ {dates[-1]}")
    except Exception as e:
        print(f"  ✗ Error loading CSV: {e}")
    
    return prices_map


def fetch_eth_prices(days=365):
    """Fetch ETH price data from CoinGecko (no API key needed, max 365 days)"""
    print(f"Fetching ETH prices from CoinGecko (last {days} days)...")
    
    params = {
        'vs_currency': 'usd',
        'days': days,
        'interval': 'daily'
    }
    
    # Retry up to 3 times with increasing delay
    for attempt in range(3):
        try:
            response = requests.get(COINGECKO_URL, params=params, timeout=30)
            print(f"  CoinGecko status code: {response.status_code}")
            
            if response.status_code == 429:
                wait_time = 30 * (attempt + 1)
                print(f"  Rate limited. Waiting {wait_time}s...")
                time.sleep(wait_time)
                continue
            
            response.raise_for_status()
            data = response.json()
            
            prices_map = {}
            prices_list = []
            
            for item in data.get('prices', []):
                timestamp = item[0] / 1000
                date = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d')
                price = round(item[1], 2)
                prices_map[date] = price
                prices_list.append({"date": date, "price": price})
            
            # Remove duplicates and sort
            seen = {}
            for p in prices_list:
                seen[p["date"]] = p
            prices_list = sorted(seen.values(), key=lambda x: x["date"])
            prices_map = {p["date"]: p["price"] for p in prices_list}
            
            print(f"  ✓ Fetched {len(prices_list)} price records")
            if prices_list:
                print(f"  Latest price: {prices_list[-1]}")
            
            return prices_map, prices_list
            
        except Exception as e:
            print(f"  Attempt {attempt + 1}/3 failed: {e}")
            if attempt < 2:
                print(f"  Waiting 15s before retry...")
                time.sleep(15)
    
    print("  ✗ All attempts failed")
    return {}, []


def merge_data(queue_data, prices_map):
    """Merge queue data with price data"""
    print("Merging queue and price data...")
    merged = []
    matched_count = 0
    
    for record in queue_data:
        date = record.get("date") or record.get("timestamp") or ""
        
        if isinstance(date, (int, float)):
            date = datetime.fromtimestamp(date).strftime("%Y-%m-%d")
        
        if isinstance(date, str) and "T" in date:
            date = date.split("T")[0]
        
        if not date:
            continue
        
        exit_queue = (
            record.get("exit_queue_eth") or 
            record.get("exitQueue") or 
            record.get("exit_queue") or 
            record.get("exit_balance") or 0
        )
        entry_queue = (
            record.get("entry_queue_eth") or 
            record.get("entryQueue") or 
            record.get("entry_queue") or 
            record.get("entry_balance") or 0
        )
        exit_wait = (
            record.get("exit_wait_days") or 
            record.get("exitWaitDays") or 0
        )
        
        eth_price = prices_map.get(date, 0)
        if eth_price > 0:
            matched_count += 1
        
        exit_queue_usd = round((exit_queue * eth_price) / 1_000_000, 2) if eth_price else 0
        
        merged.append({
            "date": date,
            "exitQueue": exit_queue,
            "entryQueue": entry_queue,
            "exitWaitDays": exit_wait,
            "ethPrice": eth_price,
            "exitQueueUSD": exit_queue_usd
        })
    
    merged.sort(key=lambda x: x["date"])
    
    print(f"  ✓ Merged {len(merged)} records")
    print(f"  ✓ Records with ETH price: {matched_count}")
    
    return merged


def calculate_stats(data):
    """Calculate summary statistics"""
    exit_queues = [d["exitQueue"] for d in data if d["exitQueue"] > 0]
    entry_queues = [d["entryQueue"] for d in data if d["entryQueue"] > 0]
    prices = [d["ethPrice"] for d in data if d["ethPrice"] > 0]
    
    if not exit_queues:
        return {}
    
    pairs = [(d["exitQueue"], d["ethPrice"]) for d in data if d["exitQueue"] > 0 and d["ethPrice"] > 0]
    correlation = 0
    if len(pairs) > 1:
        q = [p[0] for p in pairs]
        p = [p[1] for p in pairs]
        n = len(pairs)
        mean_q = sum(q) / n
        mean_p = sum(p) / n
        num = sum((q[i] - mean_q) * (p[i] - mean_p) for i in range(n))
        denom_q = sum((x - mean_q) ** 2 for x in q) ** 0.5
        denom_p = sum((x - mean_p) ** 2 for x in p) ** 0.5
        if denom_q > 0 and denom_p > 0:
            correlation = round(num / (denom_q * denom_p), 4)
    
    # Calculate net flow (entry - exit) for latest record
    current_entry = entry_queues[-1] if entry_queues else 0
    current_exit = exit_queues[-1] if exit_queues else 0
    current_net_flow = round(current_entry - current_exit, 2)
    
    return {
        "currentQueue": exit_queues[-1] if exit_queues else 0,
        "currentEntryQueue": current_entry,
        "currentNetFlow": current_net_flow,
        "avgQueue": round(sum(exit_queues) / len(exit_queues), 0),
        "maxQueue": max(exit_queues),
        "minQueue": min(exit_queues),
        "currentPrice": prices[-1] if prices else 0,
        "correlation": correlation,
        "lastUpdated": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    }


def calculate_inflection_points(data, threshold=0.5):
    """Detect inflection points"""
    points = []
    for i in range(1, len(data) - 1):
        prev = data[i - 1].get("exitQueue", 0)
        curr = data[i].get("exitQueue", 0)
        next_q = data[i + 1].get("exitQueue", 0)
        
        if prev > 0 and curr > 0:
            change_prev = (curr - prev) / prev
            change_next = (next_q - curr) / curr if curr > 0 else 0
            
            if change_prev > threshold and change_next < 0:
                points.append({
                    "date": data[i]["date"],
                    "type": "peak",
                    "exitQueue": curr,
                    "ethPrice": data[i].get("ethPrice", 0)
                })
            elif change_prev < -0.3 and change_next > 0.1:
                points.append({
                    "date": data[i]["date"],
                    "type": "trough",
                    "exitQueue": curr,
                    "ethPrice": data[i].get("ethPrice", 0)
                })
    return points


def main():
    print("=" * 60)
    print("ETH Unstaking Queue Data Fetcher")
    print(f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("Using Historical CSV + CoinGecko (free, 365 days)")
    print("=" * 60)
    
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    queue_data = fetch_validator_queue()
    if not queue_data:
        print("✗ Failed to fetch queue data")
        return 1
    
    # Step 1: Load historical prices from CSV (backfill)
    prices_map = load_historical_prices()
    
    # Step 2: Wait before CoinGecko call (rate limiting)
    print("\nWaiting 5s before CoinGecko call...")
    time.sleep(5)
    
    # Step 3: Fetch 365 days from CoinGecko and overlay (overwrites CSV for recent dates)
    coingecko_map, _ = fetch_eth_prices(days=365)
    prices_map.update(coingecko_map)  # CoinGecko takes priority for recent dates
    
    # Build final prices_list from merged map
    prices_list = sorted(
        [{"date": d, "price": p} for d, p in prices_map.items()],
        key=lambda x: x["date"]
    )
    print(f"\n  ✓ Total price records after merge: {len(prices_list)}")
    if prices_list:
        print(f"  Range: {prices_list[0]['date']} ~ {prices_list[-1]['date']}")
    
    merged_data = merge_data(queue_data, prices_map)
    
    if not merged_data:
        print("✗ No data to save")
        return 1
    
    stats = calculate_stats(merged_data)
    inflection_points = calculate_inflection_points(merged_data)
    
    output = {
        "meta": {
            "lastUpdated": stats.get("lastUpdated", ""),
            "dataSource": "ValidatorQueue.com + CoinGecko + Historical CSV",
            "recordCount": len(merged_data),
            "priceRecords": len(prices_list),
            "recordsWithPrice": sum(1 for d in merged_data if d["ethPrice"] > 0)
        },
        "stats": stats,
        "inflectionPoints": inflection_points[-20:],
        "ethPrices": prices_list,
        "data": merged_data
    }
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    print(f"✓ Data saved to {OUTPUT_FILE}")
    print(f"  Records: {len(merged_data)}")
    print(f"  Price records: {len(prices_list)}")
    print(f"  Records with price: {output['meta']['recordsWithPrice']}")
    print(f"  Current queue: {stats.get('currentQueue', 0):,.0f} ETH")
    print(f"  Current price: ${stats.get('currentPrice', 0):,.2f}")
    
    return 0


if __name__ == "__main__":
    exit(main())
