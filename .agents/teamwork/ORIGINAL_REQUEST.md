# Original User Request

## 2026-10-01T21:04:53Z

This is a single self-contained feature; keep it small and focused. 
Build a new multi-platform crawler module that pulls real job postings from external freelance/bounty platforms. The agent team should independently evaluate and select at least 2 external platforms (e.g., Gitcoin, Upwork RSS, etc.) that are easiest to pull from without complex authentication.

Working directory: /Users/lam/.gemini/antigravity/scratch/primenode-anti-agents
Integrity mode: development

## Requirements

### R1. External Crawlers
Implement fetchers for at least 2 external bounty/freelance platforms. Use standard HTTP clients (e.g., `urllib` or `requests`) and avoid heavy headless browsers if possible.

### R2. Data Normalization
Parse the raw HTML, RSS, or JSON from these platforms and normalize the jobs into the existing `IngestedBounty` dataclass format used by the system.

### R3. Integration
Integrate the new fetchers into the `LiveBountyCrawler.crawl_all()` method in `shared/live_bounty_crawler.py` so they run concurrently alongside existing fetchers.

## Acceptance Criteria

### Unit Testing & Verification
- [ ] `test_external_bounty_crawlers.py` is created and verifies parsing logic using mocked HTTP responses (no live network required for tests).
- [ ] A standalone script `tools/test_live_crawlers.py` is provided to perform a live network test and print the top 5 discovered jobs.

### Functional
- [ ] At least 2 external platforms are supported and successfully yield `IngestedBounty` instances.
- [ ] The parsed bounties include valid titles, external URLs, and non-zero reward amounts.
- [ ] `crawl_all` successfully executes the new fetchers without crashing if one platform is down.
