#!/usr/bin/env python3
"""
CIRCUIT BREAKER & DURABLE QUEUE / DLQ ENGINE v2.0
Hardened for PrimeNode Architecture (Complies with P1.6 & P1.7):
1. Universal Circuit Breaker:
   - Three canonical states: CLOSED -> OPEN -> HALF_OPEN.
   - Dual-mode: In-Memory thread-safe fallback + Redis synchronization.
   - Eliminates single-point-of-failure if Redis is unreachable.
   - Fail-closed or explicit fallback execution via .call().
2. Resilient Durable Queue & Dead Letter Queue (DLQ):
   - SQLite WAL persistent store (survives crashes, reboots, Redis downtime).
   - Redis sync when available.
   - Fencing token tracking (lease_token) preventing split-brain completions.
   - Retry budget enforcement -> automatic transition to DLQ when exhausted.
   - Stale lease reaper for dead/hanging workers.
"""

import os
import sys
import time
import json
import uuid
import sqlite3
import logging
import threading
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, Callable, List

logger = logging.getLogger("CIRCUIT_BREAKER")

DEFAULT_DB_PATH = os.environ.get("ANTI_QUEUE_DB", "/var/lib/anti-agents/durable_queue.db")
if not os.path.exists(os.path.dirname(DEFAULT_DB_PATH)) or not os.access(os.path.dirname(DEFAULT_DB_PATH), os.W_OK):
    DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "durable_queue.db")


def is_redis_listening(host: str = "127.0.0.1", port: int = 6379) -> bool:
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.02)
            s.connect((host, port))
            return True
    except Exception:
        return False


class CircuitState:
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """
    Universal Circuit Breaker:
    Operates in-memory with optional Redis synchronization.
    Guarantees state management even when Redis is down.
    """

    def __init__(self, r: Optional[Any] = None, failure_threshold: int = 3, cooldown_seconds: float = 60.0):
        self.threshold = failure_threshold
        self.cooldown = cooldown_seconds
        self._lock = threading.RLock()
        self._local_state: Dict[str, Dict[str, Any]] = {}
        if r is False:
            self.r = None
        elif r is None:
            if is_redis_listening("127.0.0.1", 6379):
                try:
                    import redis
                    self.r = redis.Redis(
                        host="127.0.0.1",
                        port=6379,
                        decode_responses=True,
                        socket_connect_timeout=0.2,
                        socket_timeout=0.2
                    )
                    self.r.ping()
                except Exception:
                    self.r = None
            else:
                self.r = None
        else:
            self.r = r

    def _get_local(self, service_key: str) -> Dict[str, Any]:
        with self._lock:
            if service_key not in self._local_state:
                self._local_state[service_key] = {
                    "state": CircuitState.CLOSED,
                    "fails": 0,
                    "tripped_at": 0.0,
                    "last_err": ""
                }
            return self._local_state[service_key]

    def get_state(self, service_key: str) -> str:
        local = self._get_local(service_key)
        now = time.time()

        # Check Redis first if available
        if self.r:
            try:
                state = self.r.get(f"anti:cb:{service_key}:state")
                if state:
                    if state == CircuitState.OPEN:
                        tripped_at = float(self.r.get(f"anti:cb:{service_key}:tripped_at") or 0)
                        if now - tripped_at > self.cooldown:
                            self.r.set(f"anti:cb:{service_key}:state", CircuitState.HALF_OPEN)
                            return CircuitState.HALF_OPEN
                    return state
            except Exception:
                pass

        # Fallback to local thread-safe state
        with self._lock:
            state = local["state"]
            if state == CircuitState.OPEN:
                if now - local["tripped_at"] > self.cooldown:
                    local["state"] = CircuitState.HALF_OPEN
                    logger.info(f"Circuit Breaker for {service_key} transitioned to HALF_OPEN (local)")
                    return CircuitState.HALF_OPEN
            return local["state"]

    def is_available(self, service_key: str) -> bool:
        state = self.get_state(service_key)
        if state in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
            return True
        return False

    def record_success(self, service_key: str):
        with self._lock:
            local = self._get_local(service_key)
            local["state"] = CircuitState.CLOSED
            local["fails"] = 0
            local["last_err"] = ""

        if self.r:
            try:
                self.r.set(f"anti:cb:{service_key}:state", CircuitState.CLOSED)
                self.r.set(f"anti:cb:{service_key}:fails", 0)
            except Exception:
                pass

    def record_failure(self, service_key: str, error_msg: str = ""):
        now = time.time()
        with self._lock:
            local = self._get_local(service_key)
            local["fails"] += 1
            local["last_err"] = error_msg
            if local["fails"] >= self.threshold or local["state"] == CircuitState.HALF_OPEN:
                local["state"] = CircuitState.OPEN
                local["tripped_at"] = now
                logger.warning(f"CIRCUIT BREAKER TRIPPED for {service_key}! State=OPEN, Cooldown={self.cooldown}s (Error: {error_msg})")

        if self.r:
            try:
                fails = self.r.incr(f"anti:cb:{service_key}:fails")
                if fails >= self.threshold or self.r.get(f"anti:cb:{service_key}:state") == CircuitState.HALF_OPEN:
                    self.r.set(f"anti:cb:{service_key}:state", CircuitState.OPEN)
                    self.r.set(f"anti:cb:{service_key}:tripped_at", now)
                    self.r.set(f"anti:cb:{service_key}:last_err", error_msg)
            except Exception:
                pass

    def trip_open(self, service_key: str, error_msg: str = "Manually tripped"):
        """Forces circuit into OPEN state immediately (used for chaos tests & emergency controls)."""
        now = time.time()
        with self._lock:
            local = self._get_local(service_key)
            local["state"] = CircuitState.OPEN
            local["fails"] = self.threshold
            local["tripped_at"] = now
            local["last_err"] = error_msg

        if self.r:
            try:
                self.r.set(f"anti:cb:{service_key}:state", CircuitState.OPEN)
                self.r.set(f"anti:cb:{service_key}:tripped_at", now)
                self.r.set(f"anti:cb:{service_key}:last_err", error_msg)
            except Exception:
                pass

    def call(self, service_key: str, func: Callable, fallback_func: Optional[Callable] = None, *args, **kwargs) -> Any:
        """
        Executes func protected by the circuit breaker.
        If OPEN: immediately calls fallback_func (or raises RuntimeError).
        If CLOSED/HALF_OPEN: executes func.
          - On success: records success.
          - On exception: records failure, then calls fallback_func (or re-raises).
        """
        if not self.is_available(service_key):
            logger.warning(f"Circuit Breaker for {service_key} is OPEN. Calling fallback route...")
            if fallback_func:
                return fallback_func(*args, **kwargs)
            raise RuntimeError(f"CircuitBreaker for '{service_key}' is OPEN (Service Unavailable)")

        try:
            res = func(*args, **kwargs)
            self.record_success(service_key)
            return res
        except Exception as e:
            self.record_failure(service_key, str(e))
            if fallback_func:
                logger.info(f"Primary call for {service_key} failed ({e}). Executing fallback route...")
                return fallback_func(*args, **kwargs)
            raise


class DurableQueue:
    """
    Persistent Job Queue & DLQ Engine:
    - Backed by SQLite WAL to guarantee durability across restarts and Redis downtime.
    - Synchronized with Redis queues when Redis is reachable.
    - Tracks fencing tokens (lease_token) to protect against zombie worker completions.
    - Automatic Dead Letter Queue (DLQ) upon exceeding max_retries.
    """

    def __init__(self, db_path: Optional[str] = None, r: Optional[Any] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self._mem_conn = None
        if self.db_path == ":memory:":
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._mem_conn.row_factory = sqlite3.Row
        else:
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)

        self._lock = threading.RLock()
        self._init_sqlite()

        if r is False:
            self.r = None
        elif r is None:
            if is_redis_listening("127.0.0.1", 6379):
                try:
                    import redis
                    self.r = redis.Redis(
                        host="127.0.0.1",
                        port=6379,
                        decode_responses=True,
                        socket_connect_timeout=0.2,
                        socket_timeout=0.2
                    )
                    self.r.ping()
                except Exception:
                    self.r = None
            else:
                self.r = None
        else:
            self.r = r

    def _get_conn(self) -> sqlite3.Connection:
        if self._mem_conn is not None:
            return self._mem_conn
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _close_conn(self, conn: sqlite3.Connection):
        if self._mem_conn is None:
            conn.close()

    def _init_sqlite(self):
        with self._lock:
            conn = self._get_conn()
            conn.execute("""
            CREATE TABLE IF NOT EXISTS queue_items (
                job_id TEXT PRIMARY KEY,
                queue_name TEXT NOT NULL,
                status TEXT NOT NULL, -- PENDING, LEASED, COMPLETED, DLQ
                payload TEXT NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 3,
                worker_id TEXT,
                lease_token TEXT,
                leased_at REAL,
                lease_duration_sec REAL DEFAULT 30.0,
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_queue_lookup ON queue_items (queue_name, status);")
            conn.commit()
            self._close_conn(conn)

    def push_job(self, queue_name: str, job_data: dict, max_retries: int = 3) -> str:
        job_id = job_data.get("job_id") or f"job-{time.time_ns()}-{uuid.uuid4().hex[:6]}"
        job_data["job_id"] = job_id
        job_data["retry_count"] = job_data.get("retry_count", 0)
        now = time.time()

        with self._lock:
            conn = self._get_conn()
            conn.execute("""
            INSERT OR REPLACE INTO queue_items
            (job_id, queue_name, status, payload, retry_count, max_retries, created_at, updated_at)
            VALUES (?, ?, 'PENDING', ?, ?, ?, ?, ?)
            """, (job_id, queue_name, json.dumps(job_data), job_data["retry_count"], max_retries, now, now))
            conn.commit()
            self._close_conn(conn)

        # Sync to Redis if available
        if self.r:
            try:
                self.r.lpush(f"anti:queue:{queue_name}", json.dumps(job_data))
            except Exception:
                pass

        return job_id

    def lease_job(self, queue_name: str, worker_id: str, lease_duration_sec: float = 30.0) -> Optional[Tuple[dict, str]]:
        """
        Atomically leases next PENDING job.
        Returns (job_data, lease_token) or None.
        """
        now = time.time()
        lease_token = f"lease_{uuid.uuid4().hex[:12]}"

        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("""
            SELECT job_id, payload, retry_count FROM queue_items
            WHERE queue_name = ? AND status = 'PENDING'
            ORDER BY created_at ASC LIMIT 1
            """, (queue_name,))
            row = cur.fetchone()
            if not row:
                self._close_conn(conn)
                return None

            job_id = row["job_id"]
            job_data = json.loads(row["payload"])
            job_data["job_id"] = job_id
            job_data["lease_token"] = lease_token
            job_data["assigned_worker"] = worker_id

            cur.execute("""
            UPDATE queue_items
            SET status = 'LEASED',
                worker_id = ?,
                lease_token = ?,
                leased_at = ?,
                lease_duration_sec = ?,
                payload = ?,
                updated_at = ?
            WHERE job_id = ? AND status = 'PENDING'
            """, (worker_id, lease_token, now, lease_duration_sec, json.dumps(job_data), now, job_id))
            conn.commit()
            self._close_conn(conn)

            return job_data, lease_token

    def pop_job(self, queue_name: str, timeout: int = 2) -> Optional[dict]:
        """Backwards compatibility helper: leases next job anonymously."""
        res = self.lease_job(queue_name, worker_id=f"worker-{uuid.uuid4().hex[:6]}", lease_duration_sec=60.0)
        if res:
            return res[0]

        # Check Redis if available
        if self.r:
            try:
                item = self.r.brpop(f"anti:queue:{queue_name}", timeout=timeout)
                if item:
                    return json.loads(item[1])
            except Exception:
                pass
        return None

    def ack_job(self, job_id: str, lease_token: str) -> bool:
        """Confirms successful completion with fencing token verification."""
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("""
            UPDATE queue_items
            SET status = 'COMPLETED',
                updated_at = ?
            WHERE job_id = ? AND status = 'LEASED' AND lease_token = ?
            """, (now, job_id, lease_token))
            affected = cur.rowcount
            conn.commit()
            self._close_conn(conn)
            return affected > 0

    def nack_job(self, job_id: str, lease_token: str, error_msg: str = "") -> bool:
        """
        Signals failure of leased job.
        Increments retry count; if retry_count >= max_retries, transitions to DLQ.
        Otherwise resets to PENDING for redelivery.
        """
        now = time.time()
        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("""
            SELECT retry_count, max_retries, payload FROM queue_items
            WHERE job_id = ? AND status = 'LEASED' AND lease_token = ?
            """, (job_id, lease_token))
            row = cur.fetchone()
            if not row:
                self._close_conn(conn)
                return False

            retries = row["retry_count"] + 1
            max_r = row["max_retries"]
            payload = json.loads(row["payload"])
            payload["retry_count"] = retries
            payload["last_error"] = error_msg

            if retries >= max_r:
                new_status = "DLQ"
                logger.warning(f"Job {job_id} exceeded max retries ({retries}/{max_r}) -> MOVED TO DLQ (Error: {error_msg})")
            else:
                new_status = "PENDING"
                logger.info(f"Job {job_id} failed attempt {retries}/{max_r} -> RE-QUEUED (Error: {error_msg})")

            cur.execute("""
            UPDATE queue_items
            SET status = ?,
                retry_count = ?,
                worker_id = NULL,
                lease_token = NULL,
                last_error = ?,
                payload = ?,
                updated_at = ?
            WHERE job_id = ?
            """, (new_status, retries, error_msg, json.dumps(payload), now, job_id))
            conn.commit()
            self._close_conn(conn)
            return True

    def handle_failure(self, job_data: dict, error_msg: str):
        """Legacy helper compatibility."""
        jid = job_data.get("job_id", "")
        token = job_data.get("lease_token", "")
        if jid and token:
            self.nack_job(jid, token, error_msg)
        elif jid:
            # Force DLQ transition
            now = time.time()
            with self._lock:
                conn = self._get_conn()
                conn.execute("""
                UPDATE queue_items
                SET status = 'DLQ', last_error = ?, updated_at = ?
                WHERE job_id = ?
                """, (error_msg, now, jid))
                conn.commit()
                self._close_conn(conn)

    def reap_stale_leases(self) -> List[str]:
        """
        Reclaims hanging/crashed worker leases where leased_at + lease_duration_sec < now.
        Returns list of reaped job_ids.
        """
        now = time.time()
        reaped = []
        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            cur.execute("""
            SELECT job_id, retry_count, max_retries, payload, lease_token
            FROM queue_items
            WHERE status = 'LEASED' AND (leased_at + lease_duration_sec) < ?
            """, (now,))
            rows = cur.fetchall()

            for r in rows:
                jid = r["job_id"]
                token = r["lease_token"]
                self.nack_job(jid, token, error_msg="Worker lease timed out / crashed")
                reaped.append(jid)

            self._close_conn(conn)
        return reaped

    def get_queue_stats(self, queue_name: Optional[str] = None) -> dict:
        with self._lock:
            conn = self._get_conn()
            cur = conn.cursor()
            if queue_name:
                cur.execute("""
                SELECT status, COUNT(*) as cnt FROM queue_items
                WHERE queue_name = ? GROUP BY status
                """, (queue_name,))
            else:
                cur.execute("SELECT status, COUNT(*) as cnt FROM queue_items GROUP BY status")

            counts = {row["status"]: row["cnt"] for row in cur.fetchall()}
            self._close_conn(conn)

        return {
            "pending": counts.get("PENDING", 0),
            "running": counts.get("LEASED", 0),
            "dlq": counts.get("DLQ", 0),
            "completed": counts.get("COMPLETED", 0)
        }


if __name__ == "__main__":
    cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=0.5)
    print("Initial CB available:", cb.is_available("test-svc"))
    cb.record_failure("test-svc", "Fail 1")
    cb.record_failure("test-svc", "Fail 2")
    cb.record_failure("test-svc", "Fail 3")
    print("After 3 fails, available (should be False):", cb.is_available("test-svc"))
    print("State:", cb.get_state("test-svc"))

    dq = DurableQueue(db_path=":memory:")
    jid = dq.push_job("tasks", {"task": "do work"})
    print(f"Pushed job {jid}")
    leased = dq.lease_job("tasks", "worker-1")
    print("Leased:", leased[0]["job_id"], "Token:", leased[1])
    ok = dq.ack_job(jid, leased[1])
    print("Acked:", ok)
    print("Queue stats:", dq.get_queue_stats())
