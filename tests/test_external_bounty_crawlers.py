#!/usr/bin/env python3
"""
Unit tests for external bounty and freelance crawlers (tests/test_external_bounty_crawlers.py).
Verifies parsing logic and fault tolerance using mocked HTTP responses (offline/no live network).
"""

import os
import sys
import json
import unittest
from unittest.mock import patch, MagicMock
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.bounty_ingestion_adapter import BountySource, IngestedBounty
from shared.external_bounty_crawlers import (
    BaseExternalCrawler,
    RemoteOKCrawler,
    WeWorkRemotelyCrawler,
    FreelancerCrawler,
    UpworkRSSCrawler,
    RemoteOKBountyAdapter,
    WeWorkRemotelyBountyAdapter,
    FreelancerBountyAdapter,
    UpworkBountyAdapter,
    _slugify,
)
from shared.live_bounty_crawler import LiveBountyCrawler


class TestBaseExternalCrawlerHelpers(unittest.TestCase):
    def setUp(self):
        self.crawler = BaseExternalCrawler(enable_network=False)

    def test_clean_html(self):
        raw = "<p>Join <strong>Acme Corp</strong> &amp; build the <em>future</em>!</p><br/>"
        cleaned = self.crawler._clean_html(raw)
        self.assertEqual(cleaned, "Join Acme Corp & build the future !")

        # Empty / None handling
        self.assertEqual(self.crawler._clean_html(""), "")
        self.assertEqual(self.crawler._clean_html(None), "")

    def test_clean_html_edge_cases(self):
        # Script and style elements should be completely stripped
        raw_script = "<p>Clean text</p><script>alert('xss');</script><style>.hidden{display:none}</style>"
        self.assertEqual(self.crawler._clean_html(raw_script), "Clean text")

        # Angle brackets in entities should not be stripped as tags
        raw_entities = "Requires C++ knowledge with std::vector&lt;int&gt; and size &lt; 10."
        self.assertEqual(self.crawler._clean_html(raw_entities), "Requires C++ knowledge with std::vector<int> and size < 10.")

        # CDATA blocks
        raw_cdata = "<![CDATA[<p>Content inside CDATA</p>]]>"
        self.assertEqual(self.crawler._clean_html(raw_cdata), "Content inside CDATA")

    def test_extract_usd_amount(self):
        # Range patterns
        self.assertEqual(self.crawler._extract_usd_amount("Budget: $30 - $250 USD"), 250.0)
        self.assertEqual(self.crawler._extract_usd_amount("Estimated range: $500 - $1,200"), 1200.0)

        # Single dollar amounts
        self.assertEqual(self.crawler._extract_usd_amount("Fixed price: $850"), 850.0)
        self.assertEqual(self.crawler._extract_usd_amount("Payout $2,500 upon delivery"), 2500.0)

        # Postfix currency
        self.assertEqual(self.crawler._extract_usd_amount("Reward of 350 USD"), 350.0)
        self.assertEqual(self.crawler._extract_usd_amount("Reward of 500 USDC"), 500.0)

        # Hourly rate
        self.assertEqual(self.crawler._extract_usd_amount("Rate: $60/hr"), 1200.0)

        # Default fallback
        self.assertEqual(self.crawler._extract_usd_amount("No budget stated", default=300.0), 300.0)
        self.assertGreater(self.crawler._extract_usd_amount(""), 0.0)

    def test_extract_usd_amount_edge_cases_and_currencies(self):
        # False-positive resistance: dates, experience ranges, version numbers must NOT match as budget
        self.assertEqual(self.crawler._extract_usd_amount("Posted on 2026-09-15. Remote job. Budget is $500."), 500.0)
        self.assertEqual(self.crawler._extract_usd_amount("Looking for 3 - 5 years experience. Salary $120,000."), 120000.0)
        self.assertEqual(self.crawler._extract_usd_amount("Migrate from v1.0 - 2.0. Budget $3,000."), 3000.0)

        # Multi-currency parsing & conversion to USD
        self.assertEqual(self.crawler._extract_usd_amount("Budget: €500 - €1,000"), 1080.0)
        self.assertEqual(self.crawler._extract_usd_amount("Budget: 500 - 1000 EUR"), 1080.0)
        self.assertEqual(self.crawler._extract_usd_amount("Fixed budget: £250"), 320.0)
        self.assertEqual(self.crawler._extract_usd_amount("(Budget: $25 - $50 AUD, Jobs: ...)"), 32.5)
        self.assertEqual(self.crawler._extract_usd_amount("Budget: ₹1500 - ₹12500 INR"), 150.0)
        self.assertEqual(self.crawler._extract_usd_amount("Rate: €50/hr"), 1080.0)
        self.assertEqual(self.crawler._extract_usd_amount("Rate: 50 EUR/hr"), 1080.0)
        self.assertEqual(self.crawler._extract_usd_amount("Rate: 40 GBP/hr"), 1024.0)

        # K-multiplier handling ($100k, 120k USD, £80k, €100k, $1.5k)
        self.assertEqual(self.crawler._extract_usd_amount("Salary: $100k - $150k"), 150000.0)
        self.assertEqual(self.crawler._extract_usd_amount("Salary: $100k"), 100000.0)
        self.assertEqual(self.crawler._extract_usd_amount("Budget: $1.5k"), 1500.0)
        self.assertEqual(self.crawler._extract_usd_amount("Budget: 120k USD"), 120000.0)
        self.assertEqual(self.crawler._extract_usd_amount("Salary: £80k"), 102400.0)
        self.assertEqual(self.crawler._extract_usd_amount("Salary: €100k"), 108000.0)

        # Venture funding and enterprise valuation immunity
        self.assertEqual(
            self.crawler._extract_usd_amount("We are a $50M backed startup looking for a contractor. Task reward is $1,500."),
            1500.0,
        )
        self.assertEqual(
            self.crawler._extract_usd_amount("Company valuation $10B. Fixed price: $3,000."),
            3000.0,
        )

        # Bare number after budget keywords (defaults to USD)
        self.assertEqual(self.crawler._extract_usd_amount("Need senior dev. Budget: 1500."), 1500.0)
        self.assertEqual(self.crawler._extract_usd_amount("Salary: 85,000."), 85000.0)

        # Extended keywords: fixed-price, prize, stipend
        self.assertEqual(self.crawler._extract_usd_amount("Fixed-price: 2500"), 2500.0)
        self.assertEqual(self.crawler._extract_usd_amount("Prize: $5,000 for winner"), 5000.0)
        self.assertEqual(self.crawler._extract_usd_amount("Stipend: $1,500"), 1500.0)
        self.assertEqual(self.crawler._extract_usd_amount("Prize pool: 10k USD"), 10000.0)

    def test_slugify_robustness(self):
        from shared.external_bounty_crawlers import _slugify
        self.assertEqual(_slugify("Acme Corp"), "acme-corp")
        self.assertEqual(_slugify("...Special...Company..."), "special-company")
        self.assertEqual(_slugify(""), "project")
        self.assertEqual(_slugify(None), "project")
        self.assertEqual(_slugify(12345), "12345")

    def test_safe_float_robustness(self):
        from shared.external_bounty_crawlers import _safe_float
        self.assertEqual(_safe_float("$120k+"), 120000.0)
        self.assertEqual(_safe_float("$150,000+"), 150000.0)
        self.assertEqual(_safe_float("$100k/yr"), 100000.0)
        self.assertEqual(_safe_float("$100k / year"), 100000.0)
        self.assertEqual(_safe_float("$140,000/annum"), 140000.0)
        self.assertEqual(_safe_float(None), 0.0)
        self.assertEqual(_safe_float("DOE"), 0.0)


class TestRemoteOKCrawler(unittest.TestCase):
    def setUp(self):
        self.crawler = RemoteOKCrawler(enable_network=False)

    def test_parse_json_valid_data(self):
        mock_payload = [
            {"legal": "This API is for personal use only"},  # Non-job header item
            {
                "id": "112233",
                "position": "Senior Backend Engineer - Python/Distributed",
                "company": "ScaleLabs",
                "url": "https://remoteok.com/remote-jobs/112233",
                "description": "<p>We are seeking a <strong>Python Engineer</strong> with asyncio experience. Payout $120,000.</p>",
                "salary_min": 100000,
                "salary_max": 140000,
                "tags": ["python", "asyncio", "distributed"],
                "location": "Worldwide",
            },
            {
                "id": "112234",
                "position": "DevOps / SRE Specialist",
                "company": "CloudForge Inc",
                "url": "/remote-jobs/112234",
                "description": "Fix kubernetes deployment pipelines and canary routing.",
                "tags": ["kubernetes", "docker"],
                "location": "US / EU",
            },
        ]

        bounties = self.crawler.parse_json(mock_payload, limit=10)
        self.assertEqual(len(bounties), 2)

        b1 = bounties[0]
        self.assertEqual(b1.bounty_id, "remoteok-112233")
        self.assertEqual(b1.platform, BountySource.REMOTEOK)
        self.assertEqual(b1.title, "Senior Backend Engineer - Python/Distributed")
        self.assertEqual(b1.raw_reward_usd, 140000.0)
        self.assertTrue(b1.issue_url.startswith("https://remoteok.com"))
        self.assertTrue(b1.is_permissive)
        self.assertIn("python", b1.tags)
        self.assertIn("remoteok", b1.tags)
        self.assertNotIn("<p>", b1.description)

        b2 = bounties[1]
        self.assertEqual(b2.bounty_id, "remoteok-112234")
        self.assertTrue(b2.issue_url.startswith("https://remoteok.com/remote-jobs/112234"))
        self.assertGreater(b2.raw_reward_usd, 0.0)

    def test_parse_json_empty_or_malformed(self):
        self.assertEqual(self.crawler.parse_json([], limit=5), [])
        self.assertEqual(self.crawler.parse_json({"not": "a list"}, limit=5), [])
        self.assertEqual(self.crawler.parse_json([{"no_position": True}], limit=5), [])

    def test_parse_json_string_and_k_salary_fields(self):
        payload = [
            {
                "id": "223344",
                "position": "Staff Distributed Systems Engineer",
                "company": "Antigravity",
                "salary_min": "120,000",
                "salary_max": "160k",
                "description": "Build high availability systems.",
            },
            {
                "id": "223345",
                "position": "Principal Architect",
                "company": "Antigravity",
                "salary_min": "DOE",
                "salary_max": "Competitive",
                "description": "Salary: $150k - $200k. Full remote.",
            },
            # Malformed item in middle should not break parser
            None,
            "corrupt string",
            {
                "id": "223346",
                "position": "Contract Rust Developer",
                "company": "RustWorks",
                "description": "Rate: $80/hr.",
            },
        ]
        bounties = self.crawler.parse_json(payload, limit=5)
        self.assertEqual(len(bounties), 3)
        self.assertEqual(bounties[0].raw_reward_usd, 160000.0)
        self.assertEqual(bounties[1].raw_reward_usd, 200000.0)
        self.assertEqual(bounties[2].raw_reward_usd, 1600.0)

    @patch.object(RemoteOKCrawler, "_fetch_raw")
    def test_fetch_bounties_offline_fallback(self, mock_fetch):
        mock_fetch.return_value = None  # Simulates network failure
        bounties = self.crawler.fetch_bounties(limit=2)
        self.assertEqual(len(bounties), 2)
        self.assertEqual(bounties[0].platform, BountySource.REMOTEOK)
        self.assertGreater(bounties[0].raw_reward_usd, 0.0)
        self.assertTrue(bounties[0].issue_url.startswith("http"))


class TestWeWorkRemotelyCrawler(unittest.TestCase):
    def setUp(self):
        self.crawler = WeWorkRemotelyCrawler(enable_network=False)

    def test_parse_rss_valid_xml(self):
        xml_data = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
            <channel>
                <title>We Work Remotely: Remote Programming Jobs</title>
                <link>https://weworkremotely.com</link>
                <item>
                    <title>Anthropic: Research Systems Engineer</title>
                    <link>https://weworkremotely.com/remote-jobs/anthropic-research-systems-engineer</link>
                    <guid>https://weworkremotely.com/remote-jobs/anthropic-research-systems-engineer</guid>
                    <category>Full-Stack Programming</category>
                    <description><![CDATA[<p>Build scalable GPU training clusters. Expected compensation: $1,500 bounty milestone.</p>]]></description>
                </item>
                <item>
                    <title>General Software Engineer</title>
                    <link>https://weworkremotely.com/remote-jobs/general-swe</link>
                    <guid>https://weworkremotely.com/remote-jobs/general-swe</guid>
                    <description>Build customer facing portals.</description>
                </item>
            </channel>
        </rss>
        """

        bounties = self.crawler.parse_rss(xml_data, limit=5)
        self.assertEqual(len(bounties), 2)

        b1 = bounties[0]
        self.assertTrue(b1.bounty_id.startswith("wwr-"))
        self.assertEqual(b1.platform, BountySource.WEWORKREMOTELY)
        self.assertEqual(b1.title, "Anthropic: Research Systems Engineer")
        self.assertEqual(b1.target_repo, "anthropic/careers")
        self.assertEqual(b1.raw_reward_usd, 1500.0)
        self.assertEqual(b1.issue_url, "https://weworkremotely.com/remote-jobs/anthropic-research-systems-engineer")
        self.assertTrue(b1.is_permissive)
        self.assertIn("weworkremotely", b1.tags)

        b2 = bounties[1]
        self.assertEqual(b2.target_repo, "weworkremotely/programming")
        self.assertGreater(b2.raw_reward_usd, 0.0)

    def test_parse_rss_malformed_xml(self):
        bounties = self.crawler.parse_rss("<<<NOT VALID XML>>>", limit=5)
        self.assertEqual(bounties, [])

    def test_parse_rss_trailing_slashes_and_query_params(self):
        xml_data = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
            <channel>
                <item>
                    <title>Acme: Trailing Slash Job</title>
                    <link>https://weworkremotely.com/remote-jobs/acme-trailing-slash-job/</link>
                    <guid>https://weworkremotely.com/remote-jobs/acme-trailing-slash-job/</guid>
                    <description>Clean description.</description>
                </item>
                <item>
                    <title>Acme: Query Param Job</title>
                    <link>https://weworkremotely.com/remote-jobs/acme-query-job?utm_source=rss&amp;ref=feed</link>
                    <guid>https://weworkremotely.com/remote-jobs/acme-query-job?utm_source=rss&amp;ref=feed</guid>
                    <description>Clean description 2.</description>
                </item>
            </channel>
        </rss>
        """
        bounties = self.crawler.parse_rss(xml_data, limit=5)
        self.assertEqual(len(bounties), 2)
        self.assertEqual(bounties[0].bounty_id, "wwr-acme-trailing-slash-job")
        self.assertEqual(bounties[1].bounty_id, "wwr-acme-query-job")
        self.assertNotEqual(bounties[0].bounty_id, bounties[1].bounty_id)

    @patch.object(WeWorkRemotelyCrawler, "_fetch_raw")
    def test_fetch_bounties_offline_fallback(self, mock_fetch):
        mock_fetch.return_value = None
        bounties = self.crawler.fetch_bounties(limit=2)
        self.assertEqual(len(bounties), 2)
        self.assertEqual(bounties[0].platform, BountySource.WEWORKREMOTELY)
        self.assertGreater(bounties[0].raw_reward_usd, 0.0)


class TestFreelancerCrawler(unittest.TestCase):
    def setUp(self):
        self.crawler = FreelancerCrawler(enable_network=False)

    def test_parse_rss_valid_xml(self):
        xml_data = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
            <channel>
                <title>Freelancer.com Projects</title>
                <item>
                    <title>Implement Real-time WebSocket Protocol Bridge</title>
                    <link>https://www.freelancer.com/projects/python/real-time-websocket-bridge.html</link>
                    <guid>Freelancer_project_998877</guid>
                    <category>Python</category>
                    <category>WebSockets</category>
                    <description><![CDATA[Need a resilient WebSocket bridge in Python. (Budget: $50 - $350 USD, Jobs: Python, AsyncIO)]]></description>
                </item>
                <item>
                    <title>WordPress Security Audit and Remediation</title>
                    <link>https://www.freelancer.com/projects/php/wordpress-security-audit.html</link>
                    <guid>Freelancer_project_998878</guid>
                    <description>Fix SQL injection vulnerabilities. Reward: $200 USD.</description>
                </item>
            </channel>
        </rss>
        """

        bounties = self.crawler.parse_rss(xml_data, limit=5)
        self.assertEqual(len(bounties), 2)

        b1 = bounties[0]
        self.assertEqual(b1.bounty_id, "fl-998877")
        self.assertEqual(b1.platform, BountySource.FREELANCER)
        self.assertEqual(b1.title, "Implement Real-time WebSocket Protocol Bridge")
        self.assertEqual(b1.raw_reward_usd, 350.0)
        self.assertEqual(b1.issue_url, "https://www.freelancer.com/projects/python/real-time-websocket-bridge.html")
        self.assertTrue(b1.is_permissive)
        self.assertIn("freelancer", b1.tags)
        self.assertIn("python", b1.tags)

        b2 = bounties[1]
        self.assertEqual(b2.bounty_id, "fl-998878")
        self.assertEqual(b2.raw_reward_usd, 200.0)

    def test_parse_rss_malformed_xml(self):
        bounties = self.crawler.parse_rss("invalid xml bytes", limit=5)
        self.assertEqual(bounties, [])

    def test_parse_rss_with_html_entities_and_multicurrency(self):
        xml_data = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
            <channel>
                <item>
                    <title>C++ &amp; Rust Distributed Systems &lt;strong&gt;Developer&lt;/strong&gt;</title>
                    <link>https://www.freelancer.com/projects/cpp/distributed.html</link>
                    <guid>Freelancer_project_555444</guid>
                    <description><![CDATA[High throughput RPC. (Budget: $25 - $50 AUD, Jobs: C++, Distributed Systems)]]></description>
                </item>
            </channel>
        </rss>
        """
        bounties = self.crawler.parse_rss(xml_data, limit=5)
        self.assertEqual(len(bounties), 1)
        b = bounties[0]
        self.assertEqual(b.title, "C++ & Rust Distributed Systems Developer")
        self.assertEqual(b.raw_reward_usd, 32.5)
        self.assertIn("distributed-systems", b.tags)


class TestUpworkRSSCrawler(unittest.TestCase):
    def setUp(self):
        self.crawler = UpworkRSSCrawler(enable_network=False)

    def test_parse_rss_upwork_format(self):
        xml_data = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
            <channel>
                <title>Upwork Jobs</title>
                <item>
                    <title>FastAPI Microservice Integration</title>
                    <link>https://www.upwork.com/jobs/~01abc123</link>
                    <guid>https://www.upwork.com/jobs/~01abc123</guid>
                    <category>Python</category>
                    <category>Backend Development</category>
                    <description><![CDATA[Looking for senior dev. <b>Budget</b>: $800. <b>Posted On</b>: October 2, 2026.]]></description>
                </item>
            </channel>
        </rss>
        """
        bounties = self.crawler.parse_rss(xml_data, limit=5)
        self.assertEqual(len(bounties), 1)
        b = bounties[0]
        self.assertEqual(b.platform, BountySource.UPWORK)
        self.assertEqual(b.title, "FastAPI Microservice Integration")
        self.assertEqual(b.raw_reward_usd, 800.0)
        self.assertTrue(b.issue_url.startswith("https://www.upwork.com"))
        self.assertTrue(b.is_permissive)
        self.assertIn("upwork", b.tags)
        self.assertIn("python", b.tags)
        self.assertIn("backend-development", b.tags)


class TestBountyAdapters(unittest.TestCase):
    def test_adapters_instantiation_and_fetch(self):
        ro_adapter = RemoteOKBountyAdapter()
        self.assertEqual(ro_adapter.source_name, BountySource.REMOTEOK)
        ro_bounties = ro_adapter.fetch_open_bounties(limit=2)
        self.assertGreater(len(ro_bounties), 0)

        ww_adapter = WeWorkRemotelyBountyAdapter()
        self.assertEqual(ww_adapter.source_name, BountySource.WEWORKREMOTELY)
        ww_bounties = ww_adapter.fetch_open_bounties(limit=2)
        self.assertGreater(len(ww_bounties), 0)

        fl_adapter = FreelancerBountyAdapter()
        self.assertEqual(fl_adapter.source_name, BountySource.FREELANCER)
        fl_bounties = fl_adapter.fetch_open_bounties(limit=2)
        self.assertGreater(len(fl_bounties), 0)

        up_adapter = UpworkBountyAdapter()
        self.assertEqual(up_adapter.source_name, BountySource.UPWORK)
        up_bounties = up_adapter.fetch_open_bounties(limit=2)
        self.assertGreater(len(up_bounties), 0)


class TestLiveBountyCrawlerIntegration(unittest.TestCase):
    def setUp(self):
        self.crawler = LiveBountyCrawler(enable_network=False)

    def test_crawl_all_includes_external_sources(self):
        bounties = self.crawler.crawl_all(limit_per_source=2)
        self.assertGreater(len(bounties), 0)

        platforms = {b.platform for b in bounties}
        self.assertIn(BountySource.REMOTEOK, platforms)
        self.assertIn(BountySource.WEWORKREMOTELY, platforms)
        self.assertIn(BountySource.FREELANCER, platforms)

        for b in bounties:
            self.assertIsInstance(b, IngestedBounty)
            self.assertTrue(len(b.title.strip()) > 0)
            self.assertTrue(b.issue_url.startswith("http"))
            self.assertGreater(b.raw_reward_usd, 0.0)
            self.assertTrue(b.is_permissive)

    @patch("shared.live_bounty_crawler.RemoteOKCrawler.fetch_bounties")
    def test_crawl_all_fault_tolerant_when_one_platform_crashes(self, mock_ro):
        # Simulate RemoteOK throwing an unexpected fatal network exception
        mock_ro.side_effect = RuntimeError("Fatal connection reset by peer")

        # crawl_all should NOT crash and should successfully return other platforms
        bounties = self.crawler.crawl_all(limit_per_source=2)
        self.assertGreater(len(bounties), 0)

        # Other platforms are present
        platforms = {b.platform for b in bounties}
        self.assertIn(BountySource.WEWORKREMOTELY, platforms)
        self.assertIn(BountySource.FREELANCER, platforms)

    def test_rank_opportunities_with_external_bounties(self):
        bounties = [
            IngestedBounty(
                bounty_id="ro-test-1",
                platform=BountySource.REMOTEOK,
                target_repo="test/repo",
                title="Test Remote Job",
                description="Test desc",
                raw_reward_usd=1000.0,
                issue_url="https://remoteok.com/1",
                currency="USD",
            ),
            IngestedBounty(
                bounty_id="ww-test-1",
                platform=BountySource.WEWORKREMOTELY,
                target_repo="test/repo2",
                title="Test WWR Job",
                description="Test desc 2",
                raw_reward_usd=500.0,
                issue_url="https://weworkremotely.com/1",
                currency="USD",
            ),
        ]
        ranked = self.crawler.rank_opportunities(bounties)
        self.assertEqual(len(ranked), 2)
        self.assertGreater(ranked[0].expected_value_usd, ranked[1].expected_value_usd)
        self.assertEqual(ranked[0].bounty.bounty_id, "ro-test-1")

    def test_fetch_upwork_bounties(self):
        upwork_bounties = self.crawler.fetch_upwork_bounties(limit=2)
        self.assertGreater(len(upwork_bounties), 0)
        self.assertEqual(upwork_bounties[0].platform, BountySource.UPWORK)
        self.assertTrue(upwork_bounties[0].issue_url.startswith("https://"))
        self.assertGreater(upwork_bounties[0].raw_reward_usd, 0.0)

    def test_relative_urls_and_empty_descriptions_normalized(self):
        # Verify Freelancer with relative link and empty description
        xml_freelancer = """<?xml version="1.0"?>
        <rss><channel><item>
            <title>Task with Relative Link</title>
            <link>/projects/python/relative-task.html</link>
            <description></description>
        </item></channel></rss>"""
        fl_c = FreelancerCrawler(enable_network=False)
        items = fl_c.parse_rss(xml_freelancer)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].issue_url.startswith("https://www.freelancer.com/projects/"))
        self.assertTrue(len(items[0].description) > 0)
        self.assertGreater(items[0].raw_reward_usd, 0.0)


if __name__ == "__main__":
    unittest.main()

