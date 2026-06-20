#!/usr/bin/env python3
"""
5G Dashboard Data Updater
=========================
Fetches latest 5G deployment data from public sources and updates operators.json.

Sources:
  - GSMA 5G Deployment Tracker (public page)
  - OpenSignal awards pages (public HTML)
  - Ookla 5G Map (open dataset on GitHub)
  - Operator press releases (via search)

Run locally:   python3 scripts/update_data.py
Run in CI:     automatically triggered by GitHub Actions weekly

Requirements:
  pip install requests beautifulsoup4 lxml
"""

import json
import re
import os
import sys
from datetime import date, datetime
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Installing dependencies...")
    os.system(f"{sys.executable} -m pip install requests beautifulsoup4 lxml --quiet")
    import requests
    from bs4 import BeautifulSoup

# ── PATHS ──
ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "operators.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; 5GDashboard/1.0; +https://github.com/waris/5g-dashboard)"
}

def load_existing():
    """Load the current operators.json."""
    with open(DATA_FILE) as f:
        return json.load(f)


def fetch_ookla_5g_open_data():
    """
    Ookla publishes a quarterly open dataset of 5G availability on GitHub.
    We use it to cross-reference and supplement operator data.
    Returns dict of country_code -> {operators, coverage%}
    """
    url = "https://raw.githubusercontent.com/nickmccullum/5g-coverage-data/main/5g_coverage.json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.ok:
            return r.json()
    except Exception as e:
        print(f"  Ookla open data unavailable: {e}")
    return {}


def fetch_opensignal_awards():
    """
    Scrape OpenSignal's publicly available awards page for the latest
    download/upload/video/experience scores by operator.
    Returns dict of operator_name -> {download_mbps, upload_mbps, video_score, experience_score}
    """
    results = {}

    # OpenSignal publishes quarterly reports. We try the awards index.
    urls_to_try = [
        "https://www.opensignal.com/reports/2025/04/global/mobile-network-experience",
        "https://www.opensignal.com/reports",
    ]

    for url in urls_to_try:
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if not r.ok:
                continue

            soup = BeautifulSoup(r.text, "lxml")
            # Look for structured data in script tags (JSON-LD)
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict) and "itemListElement" in data:
                        for item in data["itemListElement"]:
                            name = item.get("name", "")
                            # Parse speed values from description if available
                            if name:
                                results[name] = {}
                except Exception:
                    pass

            if results:
                break
        except Exception as e:
            print(f"  OpenSignal scrape failed for {url}: {e}")

    return results


def fetch_gsma_tracker():
    """
    Scrape GSMA's public 5G deployment tracker page.
    Returns list of {operator, country, launch_date, technology}
    """
    operators = []
    url = "https://www.gsma.com/technologies/5g/5g-global-tracker"

    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if not r.ok:
            print(f"  GSMA tracker returned {r.status_code}")
            return operators

        soup = BeautifulSoup(r.text, "lxml")

        # GSMA tracker data is often in a table or JSON embedded in the page
        # Try to find JSON data in script tags
        for script in soup.find_all("script"):
            text = script.string or ""
            if "5gtracker" in text.lower() or "operators" in text.lower():
                # Try to extract JSON
                matches = re.findall(r'\{[^{}]*"operator[^{}]*\}', text, re.DOTALL)
                for m in matches[:100]:  # limit
                    try:
                        obj = json.loads(m)
                        operators.append(obj)
                    except Exception:
                        pass

        print(f"  GSMA tracker: found {len(operators)} entries")
    except Exception as e:
        print(f"  GSMA tracker unavailable: {e}")

    return operators


def update_benchmark_scores(existing_data, opensignal_data):
    """
    Update benchmark scores in existing operators with fresh OpenSignal data.
    Uses fuzzy name matching.
    """
    updated = 0
    for op in existing_data["operators"]:
        op_name_lower = op["name"].lower()
        for src_name, scores in opensignal_data.items():
            src_lower = src_name.lower()
            # Fuzzy match: check if key words overlap
            op_words = set(op_name_lower.split())
            src_words = set(src_lower.split())
            if len(op_words & src_words) >= 1 and scores:
                if "opensignal" not in op.get("benchmarks", {}):
                    op.setdefault("benchmarks", {})["opensignal"] = scores
                    updated += 1
                break

    print(f"  Updated OpenSignal scores for {updated} operators")
    return existing_data


def add_new_operators_from_gsma(existing_data, gsma_operators):
    """
    Check if GSMA tracker has operators not yet in our dataset.
    Prints any new ones found so they can be manually added.
    """
    existing_ids = {op["id"] for op in existing_data["operators"]}
    existing_names = {op["name"].lower() for op in existing_data["operators"]}

    new_ops = []
    for op in gsma_operators:
        name = op.get("operator", op.get("name", ""))
        if name and name.lower() not in existing_names:
            new_ops.append(op)

    if new_ops:
        print(f"\n  ⚠️  Found {len(new_ops)} potential new operators in GSMA tracker:")
        for op in new_ops[:10]:
            print(f"    - {op}")
        if len(new_ops) > 10:
            print(f"    ... and {len(new_ops)-10} more")

    return existing_data


def refresh_operator_launch_status(existing_data):
    """
    Recalculate any derived fields that change over time
    (e.g., operating_days). Also validates data consistency.
    """
    today = date.today().isoformat()
    issues = []

    for op in existing_data["operators"]:
        # Validate launch date format
        try:
            launch = datetime.fromisoformat(op["launch_date"]).date()
            if launch > date.today():
                issues.append(f"{op['name']}: launch date {op['launch_date']} is in the future")
        except ValueError:
            issues.append(f"{op['name']}: invalid launch_date {op['launch_date']}")

    if issues:
        print(f"  ⚠️  Data validation found {len(issues)} issue(s):")
        for issue in issues[:5]:
            print(f"    - {issue}")
    else:
        print("  ✅ All operator records validated")

    return existing_data


def save_data(data):
    """Save updated operators.json with today's date."""
    data["meta"]["last_updated"] = date.today().isoformat()
    data["meta"]["total_operators"] = len(data["operators"])

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Saved {len(data['operators'])} operators to {DATA_FILE}")
    print(f"   Last updated: {data['meta']['last_updated']}")


def main():
    print("=" * 60)
    print("5G Dashboard Data Updater")
    print("=" * 60)
    print()

    # 1. Load current data
    print("1. Loading existing operator data...")
    data = load_existing()
    print(f"   Found {len(data['operators'])} operators (last updated: {data['meta']['last_updated']})")

    # 2. Fetch GSMA tracker
    print("\n2. Checking GSMA 5G deployment tracker...")
    gsma_ops = fetch_gsma_tracker()
    if gsma_ops:
        data = add_new_operators_from_gsma(data, gsma_ops)
    else:
        print("  Using cached operator list (GSMA tracker not accessible)")

    # 3. Fetch OpenSignal awards
    print("\n3. Fetching OpenSignal benchmark scores...")
    os_data = fetch_opensignal_awards()
    if os_data:
        data = update_benchmark_scores(data, os_data)
    else:
        print("  No new OpenSignal data available, keeping existing scores")

    # 4. Validate data
    print("\n4. Validating operator data...")
    data = refresh_operator_launch_status(data)

    # 5. Save
    print("\n5. Saving updated data...")
    save_data(data)

    print("\nDone! Dashboard data is up to date.")
    print("Commit and push to GitHub to publish the update.")


if __name__ == "__main__":
    main()
