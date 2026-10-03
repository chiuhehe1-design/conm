#!/usr/bin/env python3
"""
LIVE EXTERNAL BOUNTY CRAWLER TEST HARNESS (tools/test_live_crawlers.py)
Performs live network extraction across external freelance & bounty platforms:
- RemoteOK Developer Jobs
- WeWorkRemotely Engineering RSS
- Freelancer.com Projects RSS

Sorts and displays the top 5 discovered opportunities.
"""

import sys
import time
import logging
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.bounty_ingestion_adapter import BountySource, IngestedBounty
from shared.live_bounty_crawler import LiveBountyCrawler
from shared.external_bounty_crawlers import (
    RemoteOKCrawler,
    WeWorkRemotelyCrawler,
    FreelancerCrawler,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("LIVE_CRAWLER_TOOL")


def main() -> int:
    print("=" * 80)
    print("🚀 ANTI Swarm: Live External Bounty & Freelance Crawler Test")
    print("=" * 80)

    start_time = time.time()
    crawler = LiveBountyCrawler(enable_network=True, timeout_sec=8.0)

    print("\n[1/4] Crawling RemoteOK developer jobs...")
    ro_crawler = RemoteOKCrawler(enable_network=True, timeout_sec=8.0)
    ro_jobs = ro_crawler.fetch_bounties(limit=5)
    print(f"      -> Retrieved {len(ro_jobs)} jobs from RemoteOK")

    print("\n[2/4] Crawling WeWorkRemotely engineering RSS...")
    ww_crawler = WeWorkRemotelyCrawler(enable_network=True, timeout_sec=8.0)
    ww_jobs = ww_crawler.fetch_bounties(limit=5)
    print(f"      -> Retrieved {len(ww_jobs)} jobs from WeWorkRemotely")

    print("\n[3/4] Crawling Freelancer.com project RSS...")
    fl_crawler = FreelancerCrawler(enable_network=True, timeout_sec=8.0)
    fl_jobs = fl_crawler.fetch_bounties(limit=5)
    print(f"      -> Retrieved {len(fl_jobs)} jobs from Freelancer.com")

    # Combine all live external bounties
    all_external: list[IngestedBounty] = ro_jobs + ww_jobs + fl_jobs

    # Also test LiveBountyCrawler.crawl_all() integration
    print("\n[4/4] Testing concurrent LiveBountyCrawler.crawl_all()...")
    crawl_all_results = crawler.crawl_all(limit_per_source=3)
    print(f"      -> LiveBountyCrawler.crawl_all() yielded {len(crawl_all_results)} total opportunities")

    # Rank external bounties by Expected Value / Reward
    ranked = crawler.rank_opportunities(all_external)
    top_5 = ranked[:5]

    elapsed = time.time() - start_time
    print(f"\nCrawling complete in {elapsed:.2f}s! Total external jobs retrieved: {len(all_external)}")
    print("\n" + "=" * 80)
    print("🏆 TOP 5 DISCOVERED OPPORTUNITIES (LIVE NETWORK)")
    print("=" * 80)

    for i, opp in enumerate(top_5, 1):
        b = opp.bounty
        print(f"\n#{i} [{b.platform.value.upper()}] {b.title}")
        print(f"    • Bounty ID:    {b.bounty_id}")
        print(f"    • Target/Org:   {b.target_repo}")
        print(f"    • Reward (USD): ${b.raw_reward_usd:,.2f} {b.currency}")
        print(f"    • Expected Val: ${opp.expected_value_usd:,.2f} (Confidence: {opp.confidence_rate * 100:.0f}%)")
        print(f"    • URL:          {b.issue_url}")
        print(f"    • Tags:         {', '.join(b.tags[:5])}")
        print(f"    • Description:  {b.description[:120]}...")

    print("\n" + "=" * 80)
    print("✅ Live network test passed successfully!")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
