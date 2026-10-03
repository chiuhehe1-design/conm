#!/usr/bin/env python3
"""
OPPORTUNITY DEDUPLICATOR & IDEMPOTENCY ENGINE
Prevents duplicate bounty ingestion across aggregators (Bounty-Plaza, Opire, GitHub).
Provides Redis-backed distributed locks and idempotency keys to ensure:
10 events -> 1 job execution.
"""

import os
import sys
import hashlib
import sqlite3
import redis
import logging
from typing import Dict, Any, Tuple, Optional

logger = logging.getLogger("DEDUPLICATOR")
DB_PATH = "/opt/revenue_os/data/canonical_ledger.db"

def is_redis_listening(host: str = "127.0.0.1", port: int = 6379) -> bool:
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.02)
            s.connect((host, port))
            return True
    except Exception:
        return False


class Deduplicator:
    def __init__(self, redis_host="127.0.0.1", redis_port=6379, r: Optional[Any] = None):
        self._local_keys = set()
        self._local_locks = {}
        if r is False:
            self.r = None
            self.redis_available = False
        elif r is not None:
            self.r = r
            self.redis_available = bool(r)
        elif is_redis_listening(redis_host, redis_port):
            try:
                self.r = redis.Redis(
                    host=redis_host,
                    port=redis_port,
                    decode_responses=True,
                    socket_connect_timeout=0.05,
                    socket_timeout=0.05
                )
                self.r.ping()
                self.redis_available = True
            except Exception as e:
                self.r = None
                self.redis_available = False
        else:
            self.r = None
            self.redis_available = False

    def generate_fingerprint(self, platform: str, repo: str, number: int, task_str: str = "") -> str:
        norm_platform = platform.lower().strip()
        norm_repo = repo.lower().strip()
        task_hash = hashlib.sha256((task_str or f"{norm_repo}:{number}").encode()).hexdigest()[:16]
        return f"{norm_platform}:{norm_repo}:{number}:{task_hash}"

    def check_and_register_opportunity(self, platform: str, repo: str, number: int, title: str) -> Tuple[bool, Optional[str]]:
        """
        Returns (is_allowed, error_reason)
        If allowed, registers in Redis and DB.
        If duplicate, returns (False, DO_NOT_CREATE_SECOND_JOB)
        """
        cid = f"{platform}:{repo}:{number}"
        fp = self.generate_fingerprint(platform, repo, number, title)

        # 1. Check Redis fast path
        if self.redis_available:
            redis_key = f"anti:dedupe:opp:{fp}"
            if self.r.get(redis_key):
                return False, f"DO_NOT_CREATE_SECOND_JOB: Duplicate opportunity fingerprint {fp}"

        # 2. Check SQLite persistent path
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT canonical_id, stage, status FROM canonical_opportunities WHERE repo=? AND canonical_number=?", (repo, number))
            row = cur.fetchone()
            conn.close()

            if row:
                return False, f"DO_NOT_CREATE_SECOND_JOB: Already tracked as {row[0]} (stage={row[1]}, status={row[2]})"
        except Exception as e:
            logger.error(f"DB check failed in deduplicator: {e}")

        # Mark in Redis with 7 day TTL
        if self.redis_available:
            self.r.setex(f"anti:dedupe:opp:{fp}", 86400 * 7, cid)

        return True, None

    def acquire_bounty_lock(self, canonical_id: str, agent_id: str, ttl_seconds: int = 60) -> bool:
        """
        Distributed lock to ensure only one agent claims/works on a bounty.
        """
        if self.redis_available and self.r:
            try:
                lock_key = f"anti:lock:bounty:{canonical_id}"
                acquired = self.r.set(lock_key, agent_id, nx=True, ex=ttl_seconds)
                return bool(acquired)
            except Exception:
                pass

        # In-memory lock fallback
        now = time.time()
        curr = self._local_locks.get(canonical_id)
        if curr and curr["expires"] > now and curr["agent_id"] != agent_id:
            return False
        self._local_locks[canonical_id] = {"agent_id": agent_id, "expires": now + ttl_seconds}
        return True

    def release_bounty_lock(self, canonical_id: str, agent_id: str):
        if self.redis_available and self.r:
            try:
                lock_key = f"anti:lock:bounty:{canonical_id}"
                val = self.r.get(lock_key)
                if val == agent_id:
                    self.r.delete(lock_key)
            except Exception:
                pass
        curr = self._local_locks.get(canonical_id)
        if curr and curr["agent_id"] == agent_id:
            self._local_locks.pop(canonical_id, None)

    def check_idempotency_key(self, idempotency_key: str, ttl_seconds: int = 3600) -> bool:
        """
        Returns True if action is first time (allowed to proceed),
        Returns False if already executed within ttl_seconds.
        """
        if self.redis_available and self.r:
            try:
                key = f"anti:idempotency:{idempotency_key}"
                return bool(self.r.set(key, "executed", nx=True, ex=ttl_seconds))
            except Exception:
                pass

        # In-memory fallback
        if idempotency_key in self._local_keys:
            return False
        self._local_keys.add(idempotency_key)
        return True

if __name__ == "__main__":
    d = Deduplicator()
    # Test duplicate check on existing PR #4599
    allowed, err = d.check_and_register_opportunity("github", "claude-builders-bounty/claude-builders-bounty", 4599, "test")
    print(f"Check existing PR 4599: allowed={allowed}, err={err}")

    # Test lock
    got_lock = d.acquire_bounty_lock("github:test:1", "agent-01", 10)
    print(f"Lock acquired by agent-01: {got_lock}")
    second_lock = d.acquire_bounty_lock("github:test:1", "agent-02", 10)
    print(f"Lock acquired by agent-02 (should be False): {second_lock}")
    d.release_bounty_lock("github:test:1", "agent-01")
    print("Lock released")
