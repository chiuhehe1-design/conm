## 2026-10-01T21:05:23Z
You are the SWE Orchestrator (teamwork_preview_swe).

Your working directory is:
/Users/lam/.gemini/antigravity/scratch/primenode-anti-agents/.agents/teamwork/swe_1

The project root directory is:
/Users/lam/.gemini/antigravity/scratch/primenode-anti-agents

The authoritative user request is recorded in:
/Users/lam/.gemini/antigravity/scratch/primenode-anti-agents/.agents/teamwork/ORIGINAL_REQUEST.md

Task summary:
This is a single self-contained feature; keep it small and focused.
Build a new multi-platform crawler module that pulls real job postings from external freelance/bounty platforms. Evaluate and select at least 2 external platforms (e.g., Gitcoin, Upwork RSS, etc.) that are easiest to pull from without complex authentication.

Requirements:
- R1. External Crawlers: Implement fetchers for at least 2 external platforms using standard HTTP clients (e.g. urllib or requests), avoiding heavy headless browsers.
- R2. Data Normalization: Parse raw HTML, RSS, or JSON into the existing IngestedBounty dataclass format.
- R3. Integration: Integrate new fetchers into LiveBountyCrawler.crawl_all() in shared/live_bounty_crawler.py so they run concurrently alongside existing fetchers.

Acceptance Criteria:
- test_external_bounty_crawlers.py is created and verifies parsing logic using mocked HTTP responses (no live network required for unit tests).
- A standalone script tools/test_live_crawlers.py is provided to perform a live network test and print the top 5 discovered jobs.
- At least 2 external platforms supported and yield IngestedBounty instances with valid titles, external URLs, and non-zero reward amounts.
- crawl_all successfully executes new fetchers without crashing if one platform is down.

Please manage the SWE Light loop, update progress.md and BRIEFING.md in your working directory, ensure tests pass, and report back when finished.
