#!/usr/bin/env python3
"""
ETH Unstaking Queue Data Fetcher
Fetches historical validator queue data and ETH prices
Runs daily via GitHub Actions
"""

import json
import os
import requests
from datetime import datetime, timedelta
from pathlib import Path

# API endpoints
VALIDATOR_QUEUE_URL = "https://raw.githubusercontent.com/etheralpha/validatorqueue-com/main/historical_data.json"
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/coins/ethereum/market_chart"

# Output paths
DATA_DIR = Path(__file__).parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "eth_unstaking_data.json"


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


def fetch_eth_prices(days=365):
    """Fetch ETH price history from CoinGecko"""
    print(f"Fetching ETH prices (last {days} days)...")
    try:
        params = {
            "vs_currency": "usd",
            "days": days,
            "interval": "daily"
        }
        response = requests.get(COINGECKO_PRICE_URL, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        # Convert to date-price map
        prices = {}
        for timestamp, price in data.get("prices", []):
            date = datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d")
            prices[date] = round(price, 2)
        
        print(f"  ✓ Fetched {len(prices)} price records")
        return prices
    except Exception as e:
        print(f"  ✗ Error fetching prices: {e}")
        return {}


def calculate_inflection_points(data, threshold=0.5):
    """Detect inflection points where queue changes significantly"""
    points = []
    
    for i in range(1, len(data) - 1):
        prev_queue = data[i - 1].get("exitQueue", 0)
        curr_queue = data[i].get("exitQueue", 0)
        next_queue = data[i + 1].get("exitQueue", 0)
        
        if prev_queue > 0 and curr_queue > 0:
            change_from_prev = (curr_queue - prev_queue) / prev_queue
            change_to_next = (next_queue - curr_queue) / curr_queue if curr_queue > 0 else 0
            
            # Peak detection: big rise followed by decline
            if change_from_prev > threshold and change_to_next < 0:
                points.append({
                    "date": data[i]["date"],
                    "type": "peak",
                    "exitQueue": curr_queue,
                    "ethPrice": data[i].get("ethPrice", 0)
                })
            # Trough detection: decline followed by rise
            elif change_from_prev < -0.3 and change_to_next > 0.1:
                points.append({
                    "date": data[i]["date"],
                    "type": "trough",
                    "exitQueue": curr_queue,
                    "ethPrice": data[i].get("ethPrice", 0)
                })
    
    return points


def calculate_correlation(queue_values, price_values):
    """Calculate Pearson correlation coefficient"""
    n = min(len(queue_values), len(price_values))
    if n < 2:
        return 0
    
    q = queue_values[:n]
    p = price_values[:n]
    
    mean_q = sum(q) / n
    mean_p = sum(p) / n
    
    numerator = sum((q[i] - mean_q) * (p[i] - mean_p) for i in range(n))
    denom_q = sum((x - mean_q) ** 2 for x in q) ** 0.5
    denom_p = sum((x - mean_p) ** 2 for x in p) ** 0.5
    
    if denom_q == 0 or denom_p == 0:
        return 0
    
    return round(numerator / (denom_q * denom_p), 4)


def merge_data(queue_data, prices):
    """Merge queue data with price data"""
    print("Merging queue and price data...")
    merged = []
    
    for record in queue_data:
        date = record.get("date", "")
        if not date:
            continue
        
        # Extract queue values (handle different data formats)
        exit_queue = record.get("exit_queue_eth") or record.get("exitQueue") or record.get("exit_queue") or 0
        entry_queue = record.get("entry_queue_eth") or record.get("entryQueue") or record.get("entry_queue") or 0
        exit_wait = record.get("exit_wait_days") or record.get("exitWaitDays") or 0
        
        # Get price for this date
        eth_price = prices.get(date, 0)
        
        # Calculate USD value
        exit_queue_usd = round((exit_queue * eth_price) / 1_000_000, 2) if eth_price else 0
        
        merged.append({
            "date": date,
            "exitQueue": exit_queue,
            "entryQueue": entry_queue,
            "exitWaitDays": exit_wait,
            "ethPrice": eth_price,
            "exitQueueUSD": exit_queue_usd  # In millions
        })
    
    # Sort by date
    merged.sort(key=lambda x: x["date"])
    print(f"  ✓ Merged {len(merged)} records")
    return merged


def calculate_stats(data):
    """Calculate summary statistics"""
    exit_queues = [d["exitQueue"] for d in data if d["exitQueue"] > 0]
    prices = [d["ethPrice"] for d in data if d["ethPrice"] > 0]
    
    if not exit_queues:
        return {}
    
    return {
        "currentQueue": exit_queues[-1] if exit_queues else 0,
        "avgQueue": round(sum(exit_queues) / len(exit_queues), 0),
        "maxQueue": max(exit_queues),
        "minQueue": min(exit_queues),
        "correlation": calculate_correlation(exit_queues[-len(prices):], prices),
        "lastUpdated": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    }


def main():
    """Main execution"""
    print("=" * 50)
    print("ETH Unstaking Queue Data Fetcher")
    print(f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 50)
    
    # Ensure data directory exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    # Fetch data
    queue_data = fetch_validator_queue()
    prices = fetch_eth_prices(days=730)  # 2 years
    
    # Merge data
    merged_data = merge_data(queue_data, prices)
    
    if not merged_data:
        print("✗ No data to save")
        return 1
    
    # Calculate stats and inflection points
    stats = calculate_stats(merged_data)
    inflection_points = calculate_inflection_points(merged_data)
    
    # Prepare output
    output = {
        "meta": {
            "lastUpdated": stats.get("lastUpdated", ""),
            "dataSource": "ValidatorQueue.com + CoinGecko",
            "recordCount": len(merged_data)
        },
        "stats": stats,
        "inflectionPoints": inflection_points[-20:],  # Last 20 points
        "data": merged_data
    }
    
    # Save to file
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"\n✓ Data saved to {OUTPUT_FILE}")
    print(f"  Records: {len(merged_data)}")
    print(f"  Inflection points: {len(inflection_points)}")
    print(f"  Current queue: {stats.get('currentQueue', 0):,.0f} ETH")
    print(f"  Correlation: {stats.get('correlation', 0):.2%}")
    
    return 0


if __name__ == "__main__":
    exit(main())
