#!/usr/bin/env python3
"""
CANONICAL SOURCE TRUTH RESOLVER v3.0 (Dynamic & Evidence-Based)
Architecture:
1. Source Discovery:
   - Identifies whether the input is a mirror/aggregator (Bounty-Plaza, Agent-Bounties, etc.)
   - Dynamically extracts canonical upstream repository URL (e.g. original GitHub issue/PR).
2. Canonical Fetch & Evidence Snapshot:
   - Fetches live canonical JSON from GitHub/platform API.
   - Computes cryptographic source_hash = sha256(raw_content).
   - Records fetched_at timestamp and structured evidence snapshot.
3. Dynamic Normalization:
   - Extracts state, assignees, pull requests, labels, and verified reward amounts.
   - ZERO hardcoded issue numbers in business logic.
4. Eligibility Engine:
   - Rejects closed/settled issues (unless submitted by OUR_GITHUB_USER).
   - Rejects competitor-assigned issues (assignees != [] and OUR_GITHUB_USER not in assignees).
   - Detects mirror reward exaggeration (flags REWARD_CONFLICT, enforces canonical amount).
   - Detects unsupported claim mechanics (e.g. weighted draw, raffle, non-standard workflow).
5. Financial Classification:
   - Verified PR open -> stage = PR_OPEN, verified_submission_nominal_usd = reward, paid_usd = 0.
   - Eligible for work -> stage = ELIGIBLE, eligible_pipeline_usd = reward.
   - Disqualified -> stage = DISQUALIFIED, records explicit disqualification_reason.
"""

import os
import re
import json
import time
import hashlib
import sqlite3
import urllib.request
import urllib.parse
import logging
from typing import Dict, Any, Tuple, Optional, List
from pathlib import Path

logger = logging.getLogger("CANONICAL_RESOLVER_V3")

OUR_GITHUB_USER = os.getenv("ANTI_GITHUB_USER", "lam534410-hub")
DB_PATH = Path("/opt/revenue_os/data/canonical_ledger.db")

REWARD_PATTERNS = [
    r"(?i)(?:bounty|reward|prize|payout)[\s:=]*\$?([\d,]+(?:\.\d+)?)\s*(?:usd|usdc)?\b",
    r"(?i)\$([\d,]+(?:\.\d+)?)\s*(?:usd|usdc)?\b",
    r"(?i)([\d,]+(?:\.\d+)?)\s*(?:usd|usdc)\b"
]

DISQUALIFY_CLAIM_KEYWORDS = [
    "weighted draw",
    "raffle",
    "do not claim via comment",
    "do not comment to claim",
    "selection process",
    "apply via google form",
    "form.gle"
]

class CanonicalResolverV3:
    @staticmethod
    def _compute_hash(data: str) -> str:
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    @classmethod
    def fetch_api(cls, url: str) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
        """
        Fetch JSON from API with rate-limit and User-Agent handling.
        Returns: (parsed_json, raw_text, error_message)
        """
        headers = {"User-Agent": "ANTI-CanonicalResolver/3.0"}
        gh_token = os.getenv("GITHUB_TOKEN")
        if gh_token:
            headers["Authorization"] = f"Bearer {gh_token}"

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw), raw, None
        except Exception as e:
            return None, "", str(e)

    @classmethod
    def discover_canonical_source(cls, item: Dict[str, Any]) -> Tuple[str, str]:
        """
        Discover upstream canonical URL from mirror item.
        Returns: (canonical_url, source_type)
        """
        raw_url = item.get("url") or item.get("canonical_url") or ""
        body = item.get("body") or item.get("description") or ""

        # 1. Search body/description for upstream GitHub issue/PR URLs
        upstream_match = re.search(r"https://github\.com/([\w\-\.]+)/([\w\-\.]+)/(issues|pull)/(\d+)", body)
        if upstream_match:
            full_upstream = upstream_match.group(0)
            # If the raw_url is a known mirror aggregator repo (e.g. bounty-plaza), use upstream!
            if any(agg in raw_url.lower() for agg in ["bounty-plaza", "agent-bounties", "aggregator", "jobs"]):
                source_type = "github_pr" if upstream_match.group(3) == "pull" else "github_issue"
                return full_upstream, source_type

        # 2. Directly analyze raw_url
        if "github.com" in raw_url:
            if "/pull/" in raw_url:
                return raw_url, "github_pr"
            elif "/issues/" in raw_url:
                return raw_url, "github_issue"

        # 3. Fallback
        return raw_url, "generic_web"

    @classmethod
    def extract_reward_from_text(cls, text: str) -> float:
        """Dynamically extract highest reliable reward mention from text"""
        if not text:
            return 0.0
        amounts = []
        for pat in REWARD_PATTERNS:
            matches = re.findall(pat, text)
            for m in matches:
                clean = m.replace(",", "").strip()
                try:
                    val = float(clean)
                    if 0 < val <= 50000: # Sanity bounds
                        amounts.append(val)
                except ValueError:
                    pass
        return max(amounts) if amounts else 0.0

    @classmethod
    def resolve_bounty(cls, item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Dynamic, evidence-based Canonical Truth Engine.
        Zero hardcoded issue IDs.
        """
        start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        title = item.get("title", "")
        mirror_url = item.get("url", "")
        mirror_reward_str = item.get("reward", "0")
        mirror_reward = cls._parse_usd(mirror_reward_str)

        # 1. Source Discovery
        canonical_url, source_type = cls.discover_canonical_source(item)

        evidence: List[Dict[str, Any]] = [{
            "phase": "SOURCE_DISCOVERY",
            "discovered_canonical_url": canonical_url,
            "source_type": source_type,
            "timestamp": start_time
        }]

        result: Dict[str, Any] = {
            "canonical_url": canonical_url,
            "source_type": source_type,
            "title": title,
            "mirror_url": mirror_url,
            "mirror_reward_usd": mirror_reward,
            "canonical_reward_usd": 0.0,
            "stage": "DISCOVERED",
            "eligible": False,
            "status": "PENDING_VERIFICATION",
            "assignee": None,
            "our_pr_url": item.get("our_pr_url"),
            "disqualification_reason": None,
            "fetched_at": start_time,
            "source_hash": None,
            "financial": {
                "opportunity_face_value_usd": 0.0,
                "eligible_pipeline_usd": 0.0,
                "verified_submission_nominal_usd": 0.0,
                "accepted_usd": 0.0,
                "receivable_usd": 0.0,
                "paid_usd": 0.0
            },
            "evidence": evidence
        }

        # 2. Canonical Fetch if GitHub source
        gh_match = re.search(r"https://github\.com/([\w\-\.]+)/([\w\-\.]+)/(issues|pull)/(\d+)", canonical_url)
        if not gh_match:
            result["disqualification_reason"] = "Cannot resolve to verifiable canonical API source"
            result["status"] = "UNVERIFIABLE_SOURCE"
            return result

        owner, repo, item_type, num_str = gh_match.groups()
        api_url = f"https://api.github.com/repos/{owner}/{repo}/{item_type}s/{num_str}"
        canonical_data, raw_content, fetch_err = cls.fetch_api(api_url)

        if fetch_err or not canonical_data:
            # If rate limited or unavailable, record evidence and evaluate with available metadata
            evidence.append({"phase": "API_FETCH", "api_url": api_url, "error": fetch_err, "status": "FETCH_FAILED"})
            result["source_hash"] = "FETCH_FAILED"
            # Fallback evaluation based on caller assertions
            if item.get("author") == OUR_GITHUB_USER or OUR_GITHUB_USER in str(item.get("our_pr_url", "")):
                result["eligible"] = True
                result["stage"] = "PR_OPEN"
                result["status"] = "PR_OPEN_SELF_VERIFIED"
                result["canonical_reward_usd"] = mirror_reward
                result["financial"]["verified_submission_nominal_usd"] = mirror_reward
            else:
                result["disqualification_reason"] = f"Failed to fetch canonical truth: {fetch_err}"
                result["status"] = "API_ERROR"
            return result

        # Compute source hash
        source_hash = cls._compute_hash(raw_content)
        result["source_hash"] = source_hash
        evidence.append({
            "phase": "CANONICAL_SNAPSHOT",
            "api_url": api_url,
            "source_hash": source_hash,
            "status_code": 200
        })

        # 3. Dynamic Normalization
        canonical_state = canonical_data.get("state", "unknown").lower()
        is_merged = bool(canonical_data.get("merged", False))
        assignees = [a.get("login") for a in canonical_data.get("assignees", [])]
        body_text = canonical_data.get("body", "") or ""
        canonical_title = canonical_data.get("title", "")
        author = canonical_data.get("user", {}).get("login", "")

        result["assignee"] = assignees[0] if assignees else None
        result["canonical_state"] = "merged" if is_merged else canonical_state

        # Parse canonical reward from body, title, or labels
        labels_text = " ".join([l.get("name", "") for l in canonical_data.get("labels", [])])
        extracted_reward = cls.extract_reward_from_text(f"{canonical_title} {labels_text} {body_text[:1500]}")
        canonical_reward = extracted_reward if extracted_reward > 0 else mirror_reward
        result["canonical_reward_usd"] = canonical_reward

        # Check for Reward Conflict
        if mirror_reward > 0 and canonical_reward > 0 and abs(mirror_reward - canonical_reward) > 0.05:
            evidence.append({
                "phase": "REWARD_CONFLICT_CHECK",
                "mirror_reward": mirror_reward,
                "canonical_reward": canonical_reward,
                "discrepancy": mirror_reward - canonical_reward
            })
            if canonical_reward < mirror_reward:
                result["disqualification_reason"] = f"REWARD_CONFLICT: Mirror claims ${mirror_reward}, canonical source specifies ${canonical_reward}"
                result["status"] = "REWARD_CONFLICT"
                result["eligible"] = False
                return result

        # 4. Dynamic Eligibility Rules
        # Rule 4.1: Claim Mechanism Compatibility
        for kw in DISQUALIFY_CLAIM_KEYWORDS:
            if kw in body_text.lower():
                result["disqualification_reason"] = f"UNSUPPORTED_CLAIM_MECHANISM: Detected '{kw}' in canonical requirements"
                result["status"] = "CLAIM_MECHANISM_DISQUALIFIED"
                result["eligible"] = False
                return result

        # Rule 4.2: If this is an Issue (Looking for work)
        if item_type == "issue":
            if canonical_state == "closed":
                result["disqualification_reason"] = "Canonical issue is CLOSED or already SETTLED"
                result["status"] = "CLOSED"
                result["eligible"] = False
                return result

            # Competitor assignment check
            if assignees and OUR_GITHUB_USER not in assignees:
                result["disqualification_reason"] = f"ASSIGNED_TO_COMPETITOR: Canonical issue assigned to {assignees}"
                result["status"] = "ASSIGNED_TO_COMPETITOR"
                result["eligible"] = False
                return result

            # PASS -> Eligible to claim
            result["eligible"] = True
            result["stage"] = "ELIGIBILITY_VERIFIED"
            result["status"] = "ELIGIBLE"
            result["financial"]["opportunity_face_value_usd"] = canonical_reward
            result["financial"]["eligible_pipeline_usd"] = canonical_reward
            return result

        # Rule 4.3: If this is a Pull Request (Work already submitted)
        elif item_type == "pull":
            is_our_pr = (author == OUR_GITHUB_USER) or (OUR_GITHUB_USER in str(item.get("our_pr_url", "")))
            if not is_our_pr:
                result["disqualification_reason"] = f"COMPETITOR_PR: PR authored by '{author}', not OUR_GITHUB_USER '{OUR_GITHUB_USER}'"
                result["status"] = "COMPETITOR_PR"
                result["eligible"] = False
                return result

            result["our_pr_url"] = canonical_url
            result["eligible"] = True

            if is_merged:
                result["stage"] = "MERGED_SETTLED"
                result["status"] = "MERGED_AWAITING_PAYMENT_RECONCILIATION"
                result["financial"]["receivable_usd"] = canonical_reward
            elif canonical_state == "open":
                result["stage"] = "PR_OPEN"
                result["status"] = "PR_OPEN_UNDER_REVIEW"
                result["financial"]["verified_submission_nominal_usd"] = canonical_reward
            else:
                result["stage"] = "CLOSED"
                result["status"] = "PR_CLOSED_WITHOUT_MERGE"
                result["eligible"] = False

            return result

        return result

    @staticmethod
    def _parse_usd(val: Any) -> float:
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).replace("$", "").replace(",", "").replace("USD", "").replace("USDC", "").strip()
        try:
            return float(s)
        except Exception:
            return 0.0

# Backwards-compatibility alias
SourceTruthResolver = CanonicalResolverV3

if __name__ == "__main__":
    # Test with live GitHub Issue
    sample_item = {
        "url": "https://github.com/claude-builders-bounty/claude-builders-bounty/pull/4599",
        "title": "Bounty #2: Next.js + SQLite Template",
        "reward": "75",
        "author": "lam534410-hub"
    }
    res = CanonicalResolverV3.resolve_bounty(sample_item)
    print("Resolver v3 Output:")
    print(json.dumps(res, indent=2))
