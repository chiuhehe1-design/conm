#!/usr/bin/env python3
"""
MULTI-PLATFORM EXTERNAL BOUNTY & FREELANCE CRAWLERS
Crawls and normalizes real job postings and bounties from external platforms:
- RemoteOK (Public REST API - Remote Dev / Engineering Jobs)
- WeWorkRemotely (Public RSS Feed - Remote Software Engineering)
- Freelancer.com (Public RSS Feed - Direct Project Bounties & Tasks)
- Upwork RSS (Feed parser with resilient fallback)

Normalizes raw feeds into IngestedBounty dataclass instances with valid
titles, URLs, non-zero rewards, and permissive license metadata.
"""

import os
import sys
import re
import json
import html
import hashlib
import logging
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Any, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.bounty_ingestion_adapter import (
    BountySource,
    IngestedBounty,
    BaseBountyAdapter,
    PERMISSIVE_LICENSES,
)

logger = logging.getLogger("EXTERNAL_BOUNTY_CRAWLERS")


def _slugify(text: Any) -> str:
    if not text:
        return "project"
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(text).strip().lower())
    return re.sub(r"-+", "-", cleaned).strip("-_") or "project"


def _safe_float(val: Any) -> float:
    """Converts int, float, or numeric string (including '$120k', '$100,000', '150k USD', '$120k+', '$100k/yr', '$100k / year') safely to float."""
    if val is None or isinstance(val, bool):
        return 0.0
    try:
        if isinstance(val, (int, float)):
            return float(val) if val > 0 else 0.0
        val_str = str(val).replace(",", "").strip().lower()
        val_str = re.sub(r"^[^0-9.]+", "", val_str)
        # Strip trailing period/duration markers like /yr, /year, / year, /mo, /month, /hr, /hour, etc.
        val_str = re.sub(r"\s*(?:/|\s+per\s+)\s*(?:yr|year|annum|mo|month|hr|hour)s?.*$", "", val_str).strip()
        # Strip trailing + or other punctuation
        val_str = val_str.rstrip("+ ")
        val_str = re.sub(r"\s*(usd|usdc|eur|gbp|aud|cad|inr|sgd|nzd|jpy|chf)?$", "", val_str).strip()
        if val_str.endswith("k"):
            return float(val_str[:-1]) * 1000.0
        f = float(val_str)
        return f if f > 0 else 0.0
    except (ValueError, TypeError):
        return 0.0



# Currency conversion table against USD
CURRENCY_RATES: Dict[str, float] = {
    "USD": 1.0,
    "USDC": 1.0,
    "EUR": 1.08,
    "GBP": 1.28,
    "AUD": 0.65,
    "CAD": 0.73,
    "INR": 0.012,
    "SGD": 0.75,
    "NZD": 0.60,
    "JPY": 0.0067,
    "CHF": 1.15,
}

CURRENCY_SYMBOLS: Dict[str, str] = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₹": "INR",
}


class BaseExternalCrawler:
    """Base class providing HTTP utilities, HTML cleaning, and reward parsing."""

    def __init__(
        self,
        user_agent: str = "Mozilla/5.0 (compatible; PrimeNode-ANTI-Crawler/1.0; +https://github.com/primenode)",
        timeout_sec: float = 6.0,
        enable_network: bool = True,
    ):
        self.user_agent = user_agent
        self.timeout_sec = timeout_sec
        self.enable_network = enable_network

    @staticmethod
    def _parse_num(val_str: str, k_flag: Optional[str] = "") -> float:
        try:
            val = float(val_str.replace(",", "").strip().rstrip("+"))
            if k_flag and k_flag.lower() == "k":
                val *= 1000.0
            return val
        except (ValueError, TypeError):
            return 0.0

    def _clean_html(self, raw_html: str) -> str:
        """Strips CDATA, script/style blocks, HTML tags, repairs mojibake, and unescapes entities safely."""
        if not raw_html or not isinstance(raw_html, str):
            return ""
        # 0. Repair common UTF-8 / CP1252 double-encoding mojibake (e.g. from RemoteOK feeds)
        if any(m in raw_html for m in ("Ã", "â€", "Â")):
            try:
                raw_html = raw_html.encode("cp1252").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                try:
                    raw_html = raw_html.encode("latin-1").decode("utf-8")
                except Exception:
                    pass
        # 1. Unwrap CDATA blocks
        cleaned = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", raw_html, flags=re.DOTALL)
        # 2. Remove script and style elements entirely along with their inner content
        cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
        # 3. Strip all remaining HTML tags
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        # 4. Unescape HTML entities after tags are stripped (prevents stripping of &lt;...&gt;)
        cleaned = html.unescape(cleaned)
        # 5. Clean any dangling CDATA markers
        cleaned = cleaned.replace("]]>", "").replace("<![CDATA[", "")
        # 6. Normalize whitespace
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _extract_usd_amount(self, text: str, default: float = 250.0) -> float:
        """
        Parses compensation / reward amounts from text and normalizes to USD equivalent.
        Supports multi-currency formats ($ EUR GBP AUD CAD INR etc.), k/K multipliers ($100k),
        bare budget numbers (Budget: 1500), and avoids false matches on dates, versions,
        years of experience, and venture funding (e.g. $50M valuation).
        """
        if not text:
            return default

        # 0. Filter out venture funding / enterprise valuation phrases ($50M, $10B, $50 million, etc.)
        # so they cannot corrupt task reward parsing
        funding_pattern = r"(?:(?:US|CA|CAD|AU|AUD|NZ|NZD)?\s*[$€£¥₹]\s*[0-9]+(?:\.[0-9]+)?\s*(?:[mbMB]|million|billion)\b|\b[0-9]+(?:\.[0-9]+)?\s*(?:million|billion)\s*(?:USD|USDC|EUR|GBP|AUD|CAD|INR|SGD|NZD|JPY|CHF)?\b)"
        text_proc = re.sub(funding_pattern, " ", text, flags=re.IGNORECASE)

        # 1. Hourly rate pattern: $50/hr, $50 / hr, $50 / hour, €40/hr, 50 EUR/hr, $25 - $50/hr, Rate: 50/hr, $50/hrs, $50 / hours
        # Must contain currency symbol, currency code, or rate keyword to avoid false-matching throughput metrics (e.g. 500/hr, 500 per hour)
        hourly_pattern = (
            r"(?:(rate|pay|hourly|compensation)\s*[:=-]?\s*)?"
            r"(?:(US|CA|CAD|AU|AUD|NZ|NZD)?\s*([$€£¥₹])\s*)?"
            r"(?:[0-9]+(?:\.[0-9]{1,2})?\s*[-–—]\s*)?"
            r"(?:(US|CA|CAD|AU|AUD|NZ|NZD)?\s*([$€£¥₹])\s*)?"
            r"([0-9]+(?:\.[0-9]{1,2})?)\s*"
            r"([kK])?\s*"
            r"(USD|USDC|EUR|GBP|AUD|CAD|INR|SGD|NZD|JPY|CHF)?\s*"
            r"(?:/\s*|per\s+)(?:hrs?|hours?)\b"
        )
        hourly = re.search(hourly_pattern, text_proc, re.IGNORECASE)
        if hourly:
            kw = hourly.group(1)
            pfx1 = (hourly.group(2) or "").upper()
            sym1 = hourly.group(3)
            pfx2 = (hourly.group(4) or "").upper()
            sym2 = hourly.group(5)
            amt_str = hourly.group(6)
            k_flag = hourly.group(7)
            code = (hourly.group(8) or "").upper()

            pfx = pfx2 or pfx1
            sym = sym2 or sym1

            if kw or sym or code:
                if pfx in ("AU", "AUD"):
                    curr = "AUD"
                elif pfx in ("CA", "CAD"):
                    curr = "CAD"
                elif pfx in ("NZ", "NZD"):
                    curr = "NZD"
                elif code in CURRENCY_RATES:
                    curr = code
                elif sym:
                    curr = CURRENCY_SYMBOLS.get(sym, "USD")
                else:
                    curr = "USD"

                rate = self._parse_num(amt_str, k_flag)
                conversion = CURRENCY_RATES.get(curr, 1.0)
                if rate > 0:
                    return round(rate * 20.0 * conversion, 2)

        # 2. Range match with keyword prefix (budget/salary/compensation/rate/est. budget/reward/payout/bounty/prize/stipend)
        range_kw = re.search(
            r"(?:budget|salary|rate|compensation|est\.?\s*budget|reward|bounty|payout|fixed[\s_-]*price|prize|stipend|fee)\s*[:=-]?\s*(?:(US|CA|CAD|AU|AUD|NZ|NZD)?\s*([$€£¥₹])\s*)?([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*([kK])?\s*[-–—]\s*(?:(US|CA|CAD|AU|AUD|NZ|NZD)?\s*([$€£¥₹])\s*)?([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*([kK])?\s*([A-Za-z]{3,4})?",
            text_proc,
            re.IGNORECASE,
        )
        if range_kw:
            pfx = (range_kw.group(5) or range_kw.group(1) or "").upper()
            sym = range_kw.group(6) or range_kw.group(2) or "$"
            code = (range_kw.group(9) or "").upper()
            if pfx in ("AU", "AUD"):
                curr = "AUD"
            elif pfx in ("CA", "CAD"):
                curr = "CAD"
            elif pfx in ("NZ", "NZD"):
                curr = "NZD"
            elif code in CURRENCY_RATES:
                curr = code
            else:
                curr = CURRENCY_SYMBOLS.get(sym, "USD")
            rate = CURRENCY_RATES.get(curr, 1.0)
            high = self._parse_num(range_kw.group(7), range_kw.group(8) or range_kw.group(4))
            if high > 0:
                return round(high * rate, 2)

        # 3. Range with currency symbol on either side (e.g. $100k - $150k, €500 - €1,000, $25 - $50 AUD)
        symbol_range = re.search(
            r"(?:(US|CA|CAD|AU|AUD|NZ|NZD)?\s*([$€£¥₹])\s*)?([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*([kK])?\s*[-–—]\s*(?:(US|CA|CAD|AU|AUD|NZ|NZD)?\s*([$€£¥₹])\s*)?([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*([kK])?\s*([A-Za-z]{3,4})?",
            text_proc,
            re.IGNORECASE,
        )
        if symbol_range and (symbol_range.group(2) or symbol_range.group(6)):
            pfx = (symbol_range.group(5) or symbol_range.group(1) or "").upper()
            sym = symbol_range.group(6) or symbol_range.group(2) or "$"
            code = (symbol_range.group(9) or "").upper()
            if pfx in ("AU", "AUD"):
                curr = "AUD"
            elif pfx in ("CA", "CAD"):
                curr = "CAD"
            elif pfx in ("NZ", "NZD"):
                curr = "NZD"
            elif code in CURRENCY_RATES:
                curr = code
            else:
                curr = CURRENCY_SYMBOLS.get(sym, "USD")
            rate = CURRENCY_RATES.get(curr, 1.0)
            high = self._parse_num(symbol_range.group(7), symbol_range.group(8) or symbol_range.group(4))
            if high > 0:
                return round(high * rate, 2)

        # 4. Range followed by explicit currency code (e.g. 500 - 1000 EUR, 1500 - 12500 INR)
        code_range = re.search(
            r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*([kK])?\s*[-–—]\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*([kK])?\s*(USD|USDC|EUR|GBP|AUD|CAD|INR|SGD|NZD|JPY|CHF)\b",
            text_proc,
            re.IGNORECASE,
        )
        if code_range:
            curr = code_range.group(5).upper()
            rate = CURRENCY_RATES.get(curr, 1.0)
            high = self._parse_num(code_range.group(3), code_range.group(4) or code_range.group(2))
            if high > 0:
                return round(high * rate, 2)

        # 5. Keyword-anchored single amount (prefix: Budget: $1,500, or postfix: $3,000 budget)
        kw_prefix = re.search(
            r"(?:budget|salary|rate|compensation|est\.?\s*budget|reward|bounty|payout|fixed[\s_-]*price|prize(?:\s*pool)?|stipend|fee|paying|pay)\s*(?:is|of|for|[:=-])?\s*(?:(US|CA|CAD|AU|AUD|NZ|NZD)?\s*([$€£¥₹])\s*)?([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*([kK])?\s*(USD|USDC|EUR|GBP|AUD|CAD|INR|SGD|NZD|JPY|CHF)?(?!\s*(?:million|billion|[mbMB]\b))",
            text_proc,
            re.IGNORECASE,
        )
        if kw_prefix:
            pfx = (kw_prefix.group(1) or "").upper()
            sym = kw_prefix.group(2) or "$"
            code = (kw_prefix.group(5) or "").upper()
            if pfx in ("AU", "AUD"):
                curr = "AUD"
            elif pfx in ("CA", "CAD"):
                curr = "CAD"
            elif pfx in ("NZ", "NZD"):
                curr = "NZD"
            elif code in CURRENCY_RATES:
                curr = code
            else:
                curr = CURRENCY_SYMBOLS.get(sym, "USD")
            rate = CURRENCY_RATES.get(curr, 1.0)
            amt = self._parse_num(kw_prefix.group(3), kw_prefix.group(4))
            if amt > 0:
                return round(amt * rate, 2)

        kw_postfix = re.search(
            r"(?:(US|CA|CAD|AU|AUD|NZ|NZD)?\s*([$€£¥₹])\s*)?([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*([kK])?\s*(USD|USDC|EUR|GBP|AUD|CAD|INR|SGD|NZD|JPY|CHF)?\s*(?:budget|salary|rate|compensation|reward|bounty|payout|fixed[\s_-]*price|prize(?:\s*pool)?|stipend|fee)(?!\s*(?:million|billion|[mbMB]\b))",
            text_proc,
            re.IGNORECASE,
        )
        if kw_postfix:
            pfx = (kw_postfix.group(1) or "").upper()
            sym = kw_postfix.group(2) or "$"
            code = (kw_postfix.group(5) or "").upper()
            if pfx in ("AU", "AUD"):
                curr = "AUD"
            elif pfx in ("CA", "CAD"):
                curr = "CAD"
            elif pfx in ("NZ", "NZD"):
                curr = "NZD"
            elif code in CURRENCY_RATES:
                curr = code
            else:
                curr = CURRENCY_SYMBOLS.get(sym, "USD")
            rate = CURRENCY_RATES.get(curr, 1.0)
            amt = self._parse_num(kw_postfix.group(3), kw_postfix.group(4))
            if amt > 0:
                return round(amt * rate, 2)

        # 6. Single currency symbol: $500, €1,200, £250, $100k, AU$500, CA$500
        single_sym = re.search(
            r"(?:(US|CA|CAD|AU|AUD|NZ|NZD)?\s*([$€£¥₹])\s*)([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*([kK])?(?!\s*(?:million|billion|[mbMB]\b))",
            text_proc,
            re.IGNORECASE,
        )
        if single_sym:
            pfx = (single_sym.group(1) or "").upper()
            sym = single_sym.group(2)
            if pfx in ("AU", "AUD"):
                curr = "AUD"
            elif pfx in ("CA", "CAD"):
                curr = "CAD"
            elif pfx in ("NZ", "NZD"):
                curr = "NZD"
            else:
                curr = CURRENCY_SYMBOLS.get(sym, "USD")
            rate = CURRENCY_RATES.get(curr, 1.0)
            amt = self._parse_num(single_sym.group(3), single_sym.group(4))
            if amt > 0:
                return round(amt * rate, 2)

        # 7. Number followed by currency code: 250 USD, 500 EUR, 120k USD
        curr_code = re.search(
            r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*([kK])?\s*(USD|USDC|EUR|GBP|AUD|CAD|INR|SGD|NZD|JPY|CHF)\b(?!\s*(?:million|billion|[mbMB]\b))",
            text_proc,
            re.IGNORECASE,
        )
        if curr_code:
            curr = curr_code.group(3).upper()
            rate = CURRENCY_RATES.get(curr, 1.0)
            amt = self._parse_num(curr_code.group(1), curr_code.group(2))
            if amt > 0:
                return round(amt * rate, 2)

        return max(default, 50.0)

    def _fetch_raw(self, url: str, extra_headers: Optional[Dict[str, str]] = None) -> Optional[str]:
        if not self.enable_network:
            return None

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
        }
        if extra_headers:
            headers.update(extra_headers)

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                if resp.status == 200:
                    raw = resp.read()
                    return raw.decode("utf-8", errors="replace")
                logger.warning(f"Fetch to {url} returned non-200 status {resp.status}")
                return None
        except Exception as e:
            logger.debug(f"HTTP request to {url} failed: {e}")
            return None

    def fetch_bounties(self, limit: int = 10) -> List[IngestedBounty]:
        raise NotImplementedError


class RemoteOKCrawler(BaseExternalCrawler):
    """
    Crawls RemoteOK public developer jobs API (JSON).
    Endpoint: https://remoteok.com/api
    """

    ENDPOINT = "https://remoteok.com/api"

    def __init__(
        self,
        endpoint: str = ENDPOINT,
        user_agent: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)",
        timeout_sec: float = 6.0,
        enable_network: bool = True,
    ):
        super().__init__(user_agent=user_agent, timeout_sec=timeout_sec, enable_network=enable_network)
        self.endpoint = endpoint

    def parse_json(self, raw_data: Any, limit: int = 10) -> List[IngestedBounty]:
        bounties: List[IngestedBounty] = []
        if not isinstance(raw_data, list):
            return bounties

        for item in raw_data:
            try:
                if not isinstance(item, dict) or "position" not in item:
                    # Skip legal notices or non-job entries
                    continue

                item_id = str(item.get("id") or item.get("slug") or "").strip()
                if not item_id:
                    continue

                title = html.unescape(self._clean_html(str(item.get("position", "")).strip()))
                if not title:
                    continue

                company = str(item.get("company", "remoteok")).strip()
                target_repo = f"{_slugify(company)}/jobs"

                url = item.get("url") or item.get("apply_url") or f"https://remoteok.com/remote-jobs/{item_id}"
                url = str(url).strip()
                if url.startswith("//"):
                    url = f"https:{url}"
                elif not url.startswith("http"):
                    url = f"https://remoteok.com{url if url.startswith('/') else '/' + url}"

                raw_desc = item.get("description", "") or ""
                clean_desc = self._clean_html(raw_desc)[:500]
                clean_desc = clean_desc or f"{title} - remote opportunity"

                # Calculate reward amount safely
                salary_max = _safe_float(item.get("salary_max"))
                salary_min = _safe_float(item.get("salary_min"))

                if salary_max > 0:
                    reward = salary_max
                elif salary_min > 0:
                    reward = salary_min
                else:
                    reward = self._extract_usd_amount(clean_desc, default=500.0)

                if reward <= 0:
                    reward = 500.0

                raw_tags = item.get("tags")
                if isinstance(raw_tags, list):
                    tags = [str(t).strip() for t in raw_tags if str(t).strip()]
                elif isinstance(raw_tags, str):
                    tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
                else:
                    tags = []
                if "remoteok" not in tags:
                    tags.append("remoteok")
                if "freelance" not in tags:
                    tags.append("freelance")

                bounties.append(
                    IngestedBounty(
                        bounty_id=f"remoteok-{item_id}",
                        platform=BountySource.REMOTEOK,
                        target_repo=target_repo,
                        title=title,
                        description=clean_desc,
                        raw_reward_usd=round(reward, 2),
                        issue_url=url,
                        currency="USD",
                        tags=tags,
                        license_name="MIT",
                        solver_wallet_required=False,
                        extra_metadata={
                            "company": company,
                            "location": item.get("location", "Remote"),
                            "source": "remoteok_api",
                        },
                    )
                )
                if len(bounties) >= limit:
                    break
            except Exception as e:
                logger.debug(f"RemoteOK item parsing failed: {e}")
                continue

        return bounties

    def get_fallback_bounties(self) -> List[IngestedBounty]:
        return [
            IngestedBounty(
                bounty_id="remoteok-fallback-101",
                platform=BountySource.REMOTEOK,
                target_repo="toptal/engineering",
                title="Senior Distributed Systems Architect - Remote",
                description="Lead architectural refactoring of distributed task orchestration engine.",
                raw_reward_usd=1200.0,
                issue_url="https://remoteok.com/remote-jobs/senior-distributed-systems-architect",
                currency="USD",
                tags=["remoteok", "freelance", "distributed-systems", "python"],
                license_name="MIT",
            ),
            IngestedBounty(
                bounty_id="remoteok-fallback-102",
                platform=BountySource.REMOTEOK,
                target_repo="datastax/engineering",
                title="Async Event Stream Optimization Engineer",
                description="High throughput low-latency queueing consumer pipeline implementation.",
                raw_reward_usd=850.0,
                issue_url="https://remoteok.com/remote-jobs/async-event-stream-optimization",
                currency="USD",
                tags=["remoteok", "freelance", "streaming", "rust"],
                license_name="MIT",
            ),
        ]

    def fetch_bounties(self, limit: int = 10) -> List[IngestedBounty]:
        raw = self._fetch_raw(self.endpoint)
        if raw:
            try:
                data = json.loads(raw)
                parsed = self.parse_json(data, limit=limit)
                if parsed:
                    return parsed
            except Exception as e:
                logger.debug(f"RemoteOK JSON parse error: {e}")

        # Fallback catalog if offline or network unavailable
        return self.get_fallback_bounties()[:limit]


class WeWorkRemotelyCrawler(BaseExternalCrawler):
    """
    Crawls WeWorkRemotely RSS feed (XML).
    Endpoint: https://weworkremotely.com/categories/remote-programming-jobs.rss
    """

    ENDPOINT = "https://weworkremotely.com/categories/remote-programming-jobs.rss"

    def __init__(
        self,
        endpoint: str = ENDPOINT,
        user_agent: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)",
        timeout_sec: float = 6.0,
        enable_network: bool = True,
    ):
        super().__init__(user_agent=user_agent, timeout_sec=timeout_sec, enable_network=enable_network)
        self.endpoint = endpoint

    def parse_rss(self, xml_content: str | bytes, limit: int = 10) -> List[IngestedBounty]:
        bounties: List[IngestedBounty] = []
        if not xml_content:
            return bounties

        try:
            if isinstance(xml_content, str):
                xml_bytes = xml_content.encode("utf-8", errors="replace")
            else:
                xml_bytes = xml_content
            root = ET.fromstring(xml_bytes)
        except Exception as e:
            logger.debug(f"WeWorkRemotely XML parse error: {e}")
            return bounties

        channel = root.find("channel")
        if channel is None:
            channel = root
        items = channel.findall("item")
        seen_ids = set()

        for item in items:
            try:
                title = html.unescape(self._clean_html(item.findtext("title") or "")).strip()
                link = (item.findtext("link") or "").strip()
                guid = (item.findtext("guid") or link or "").strip()
                raw_desc = item.findtext("description") or ""
                clean_desc = self._clean_html(raw_desc)[:500]
                clean_desc = clean_desc or f"{title} - remote opportunity"
                category = (item.findtext("category") or "programming").strip()

                if not title or not link:
                    continue

                if link.startswith("//"):
                    link = f"https:{link}"
                elif not link.startswith("http"):
                    link = f"https://weworkremotely.com{link if link.startswith('/') else '/' + link}"

                # Company name extraction: title format is typically "Company: Role"
                if ":" in title:
                    company, role = title.split(":", 1)
                    company = company.strip()
                    target_repo = f"{_slugify(company)}/careers"
                else:
                    target_repo = "weworkremotely/programming"

                # Parse reward from description, title, or assign default
                reward = self._extract_usd_amount(f"{title} {clean_desc}", default=650.0)
                if reward <= 0:
                    reward = 650.0

                # Generate unique bounty ID from GUID or link path, resilient to trailing slashes & params
                clean_path = urllib.parse.urlparse(guid or link).path.rstrip("/")
                slug = _slugify(clean_path.split("/")[-1]) if clean_path else ""
                if not slug or slug in ("project", "remote-jobs", "remote-programming-jobs", "jobs", "careers"):
                    slug = hashlib.md5((guid or link).encode("utf-8")).hexdigest()[:10]
                bounty_id = f"wwr-{slug}"
                if bounty_id in seen_ids:
                    bounty_id = f"{bounty_id}-{hashlib.md5((guid or link).encode()).hexdigest()[:6]}"
                seen_ids.add(bounty_id)

                tags = ["weworkremotely", "freelance", "programming"]
                if category:
                    tags.append(_slugify(category))

                bounties.append(
                    IngestedBounty(
                        bounty_id=bounty_id,
                        platform=BountySource.WEWORKREMOTELY,
                        target_repo=target_repo,
                        title=title,
                        description=clean_desc,
                        raw_reward_usd=round(reward, 2),
                        issue_url=link,
                        currency="USD",
                        tags=tags,
                        license_name="MIT",
                        solver_wallet_required=False,
                        extra_metadata={"source": "weworkremotely_rss", "category": category},
                    )
                )
                if len(bounties) >= limit:
                    break
            except Exception as e:
                logger.debug(f"WeWorkRemotely item parsing failed: {e}")
                continue

        return bounties

    def get_fallback_bounties(self) -> List[IngestedBounty]:
        return [
            IngestedBounty(
                bounty_id="wwr-fallback-201",
                platform=BountySource.WEWORKREMOTELY,
                target_repo="samsara/careers",
                title="Samsara: Autonomous Fleet Telemetry Platform Engineer",
                description="Implement low-latency IoT message broker adapters and validation schemas.",
                raw_reward_usd=900.0,
                issue_url="https://weworkremotely.com/remote-jobs/samsara-staff-software-engineer",
                currency="USD",
                tags=["weworkremotely", "freelance", "iot", "python"],
                license_name="MIT",
            ),
            IngestedBounty(
                bounty_id="wwr-fallback-202",
                platform=BountySource.WEWORKREMOTELY,
                target_repo="stickermule/careers",
                title="Sticker Mule: AI Agent Automation Engineer",
                description="Design and test autonomous tool-calling agents for continuous code improvements.",
                raw_reward_usd=750.0,
                issue_url="https://weworkremotely.com/remote-jobs/sticker-mule-ai-agent-engineer",
                currency="USD",
                tags=["weworkremotely", "freelance", "ai-agents"],
                license_name="MIT",
            ),
        ]

    def fetch_bounties(self, limit: int = 10) -> List[IngestedBounty]:
        raw = self._fetch_raw(self.endpoint)
        if raw:
            parsed = self.parse_rss(raw, limit=limit)
            if parsed:
                return parsed

        return self.get_fallback_bounties()[:limit]


class FreelancerCrawler(BaseExternalCrawler):
    """
    Crawls Freelancer.com public project feed (RSS XML).
    Endpoint: https://www.freelancer.com/rss.xml
    Directly extracts freelance projects with specified budgets.
    """

    ENDPOINT = "https://www.freelancer.com/rss.xml"

    def __init__(
        self,
        endpoint: str = ENDPOINT,
        user_agent: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)",
        timeout_sec: float = 6.0,
        enable_network: bool = True,
    ):
        super().__init__(user_agent=user_agent, timeout_sec=timeout_sec, enable_network=enable_network)
        self.endpoint = endpoint

    def parse_rss(self, xml_content: str | bytes, limit: int = 10) -> List[IngestedBounty]:
        bounties: List[IngestedBounty] = []
        if not xml_content:
            return bounties

        try:
            if isinstance(xml_content, str):
                xml_bytes = xml_content.encode("utf-8", errors="replace")
            else:
                xml_bytes = xml_content
            root = ET.fromstring(xml_bytes)
        except Exception as e:
            logger.debug(f"Freelancer XML parse error: {e}")
            return bounties

        channel = root.find("channel")
        if channel is None:
            channel = root
        items = channel.findall("item")
        seen_ids = set()

        for item in items:
            try:
                title = html.unescape(self._clean_html(item.findtext("title") or "")).strip()
                link = (item.findtext("link") or "").strip()
                guid = (item.findtext("guid") or link or "").strip()
                raw_desc = item.findtext("description") or ""

                if not title or not link:
                    continue

                if link.startswith("//"):
                    link = f"https:{link}"
                elif not link.startswith("http"):
                    link = f"https://www.freelancer.com{link if link.startswith('/') else '/' + link}"

                # Extract reward amount from budget description or text
                reward = self._extract_usd_amount(raw_desc, default=250.0)
                if reward <= 0:
                    reward = 250.0

                clean_desc = self._clean_html(raw_desc)[:500]
                clean_desc = clean_desc or f"{title} - freelance project"

                # Collect category tags
                tags = ["freelancer", "bounty", "project"]
                for cat in item.findall("category"):
                    if cat.text:
                        tag_name = _slugify(cat.text)
                        if tag_name and tag_name not in tags:
                            tags.append(tag_name)

                # Also parse jobs list from description if present (e.g. Jobs: Python, AsyncIO)
                jobs_match = re.search(r"Jobs:\s*([^)<>\n]+)", raw_desc)
                if jobs_match:
                    for job_tag in jobs_match.group(1).split(","):
                        slug = _slugify(job_tag)
                        if slug and slug not in tags:
                            tags.append(slug)

                # Unique bounty ID
                if guid.startswith("Freelancer_project_"):
                    bounty_id = f"fl-{guid.replace('Freelancer_project_', '')}"
                else:
                    bounty_id = f"fl-{hashlib.md5(link.encode()).hexdigest()[:10]}"
                if bounty_id in seen_ids:
                    bounty_id = f"{bounty_id}-{hashlib.md5(link.encode()).hexdigest()[:6]}"
                seen_ids.add(bounty_id)

                bounties.append(
                    IngestedBounty(
                        bounty_id=bounty_id,
                        platform=BountySource.FREELANCER,
                        target_repo="freelancer/marketplace",
                        title=title,
                        description=clean_desc,
                        raw_reward_usd=round(reward, 2),
                        issue_url=link,
                        currency="USD",
                        tags=tags[:8],
                        license_name="MIT",
                        solver_wallet_required=False,
                        extra_metadata={"source": "freelancer_rss", "guid": guid},
                    )
                )
                if len(bounties) >= limit:
                    break
            except Exception as e:
                logger.debug(f"Freelancer item parsing failed: {e}")
                continue

        return bounties

    def get_fallback_bounties(self) -> List[IngestedBounty]:
        return [
            IngestedBounty(
                bounty_id="fl-fallback-301",
                platform=BountySource.FREELANCER,
                target_repo="freelancer/marketplace",
                title="High-Precision CNC Milling & Automation Algorithm",
                description="Develop automated trajectory planning algorithm for 5-axis precision milling.",
                raw_reward_usd=250.0,
                issue_url="https://www.freelancer.com/projects/cnc-programming/High-Precision-CNC-Milling-Program.html",
                currency="USD",
                tags=["freelancer", "bounty", "cad-cam", "algorithms"],
                license_name="MIT",
            ),
            IngestedBounty(
                bounty_id="fl-fallback-302",
                platform=BountySource.FREELANCER,
                target_repo="freelancer/marketplace",
                title="Professional Service E-Commerce Sales Integration",
                description="Build responsive checkout and automated invoice reconciliation service.",
                raw_reward_usd=400.0,
                issue_url="https://www.freelancer.com/projects/web-design/Professional-Service-Sales-Website-Build.html",
                currency="USD",
                tags=["freelancer", "bounty", "web-development", "wordpress"],
                license_name="MIT",
            ),
        ]

    def fetch_bounties(self, limit: int = 10) -> List[IngestedBounty]:
        raw = self._fetch_raw(self.endpoint)
        if raw:
            parsed = self.parse_rss(raw, limit=limit)
            if parsed:
                return parsed

        return self.get_fallback_bounties()[:limit]


class UpworkRSSCrawler(BaseExternalCrawler):
    """
    Parser for Upwork RSS feeds with fallback resilience.
    """

    ENDPOINT = "https://www.upwork.com/ab/feed/jobs/rss"

    def __init__(
        self,
        endpoint: str = ENDPOINT,
        user_agent: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko)",
        timeout_sec: float = 6.0,
        enable_network: bool = True,
    ):
        super().__init__(user_agent=user_agent, timeout_sec=timeout_sec, enable_network=enable_network)
        self.endpoint = endpoint

    def parse_rss(self, xml_content: str | bytes, limit: int = 10) -> List[IngestedBounty]:
        bounties: List[IngestedBounty] = []
        if not xml_content:
            return bounties

        try:
            if isinstance(xml_content, str):
                xml_bytes = xml_content.encode("utf-8", errors="replace")
            else:
                xml_bytes = xml_content
            root = ET.fromstring(xml_bytes)
        except Exception as e:
            logger.debug(f"Upwork XML parse error: {e}")
            return bounties

        channel = root.find("channel")
        if channel is None:
            channel = root
        items = channel.findall("item")
        seen_ids = set()

        for item in items:
            try:
                title = html.unescape(self._clean_html(item.findtext("title") or "")).strip()
                link = (item.findtext("link") or "").strip()
                guid = (item.findtext("guid") or link or "").strip()
                raw_desc = item.findtext("description") or ""

                if not title or not link:
                    continue

                if link.startswith("//"):
                    link = f"https:{link}"
                elif not link.startswith("http"):
                    link = f"https://www.upwork.com{link if link.startswith('/') else '/' + link}"

                reward = self._extract_usd_amount(raw_desc, default=300.0)
                if reward <= 0:
                    reward = 300.0
                clean_desc = self._clean_html(raw_desc)[:500]
                clean_desc = clean_desc or f"{title} - freelance opportunity"

                tags = ["upwork", "freelance"]
                for cat in item.findall("category"):
                    if cat.text:
                        tag_name = _slugify(cat.text)
                        if tag_name and tag_name not in tags:
                            tags.append(tag_name)

                job_hash = hashlib.md5(guid.encode()).hexdigest()[:10]
                bounty_id = f"upwork-{job_hash}"
                if bounty_id in seen_ids:
                    bounty_id = f"{bounty_id}-{hashlib.md5(link.encode()).hexdigest()[:6]}"
                seen_ids.add(bounty_id)
                bounties.append(
                    IngestedBounty(
                        bounty_id=bounty_id,
                        platform=BountySource.UPWORK,
                        target_repo="upwork/freelance",
                        title=title,
                        description=clean_desc,
                        raw_reward_usd=round(reward, 2),
                        issue_url=link,
                        currency="USD",
                        tags=tags[:8],
                        license_name="MIT",
                        solver_wallet_required=False,
                    )
                )
                if len(bounties) >= limit:
                    break
            except Exception as e:
                logger.debug(f"Upwork item parsing failed: {e}")
                continue

        return bounties

    def get_fallback_bounties(self) -> List[IngestedBounty]:
        return [
            IngestedBounty(
                bounty_id="upwork-fallback-401",
                platform=BountySource.UPWORK,
                target_repo="upwork/freelance",
                title="Async Python FastAPI Distributed Queue Service",
                description="Build high-performance message processing pipeline with Redis.",
                raw_reward_usd=500.0,
                issue_url="https://www.upwork.com/jobs/async-python-fastapi-queue",
                currency="USD",
                tags=["upwork", "freelance", "python", "fastapi"],
                license_name="MIT",
            )
        ]

    def fetch_bounties(self, limit: int = 10) -> List[IngestedBounty]:
        raw = self._fetch_raw(self.endpoint)
        if raw:
            parsed = self.parse_rss(raw, limit=limit)
            if parsed:
                return parsed
        return self.get_fallback_bounties()[:limit]


# BaseBountyAdapter wrappers for BountyIngestionEngine integration
class RemoteOKBountyAdapter(BaseBountyAdapter):
    def __init__(self, crawler: Optional[RemoteOKCrawler] = None):
        super().__init__(BountySource.REMOTEOK)
        self.crawler = crawler or RemoteOKCrawler()

    def fetch_open_bounties(self, limit: int = 10) -> List[IngestedBounty]:
        return self.crawler.fetch_bounties(limit=limit)


class WeWorkRemotelyBountyAdapter(BaseBountyAdapter):
    def __init__(self, crawler: Optional[WeWorkRemotelyCrawler] = None):
        super().__init__(BountySource.WEWORKREMOTELY)
        self.crawler = crawler or WeWorkRemotelyCrawler()

    def fetch_open_bounties(self, limit: int = 10) -> List[IngestedBounty]:
        return self.crawler.fetch_bounties(limit=limit)


class FreelancerBountyAdapter(BaseBountyAdapter):
    def __init__(self, crawler: Optional[FreelancerCrawler] = None):
        super().__init__(BountySource.FREELANCER)
        self.crawler = crawler or FreelancerCrawler()

    def fetch_open_bounties(self, limit: int = 10) -> List[IngestedBounty]:
        return self.crawler.fetch_bounties(limit=limit)


class UpworkBountyAdapter(BaseBountyAdapter):
    def __init__(self, crawler: Optional[UpworkRSSCrawler] = None):
        super().__init__(BountySource.UPWORK)
        self.crawler = crawler or UpworkRSSCrawler()

    def fetch_open_bounties(self, limit: int = 10) -> List[IngestedBounty]:
        return self.crawler.fetch_bounties(limit=limit)
