#!/usr/bin/env python3
"""
CLI TOOL: DISASTER RECOVERY & INTEGRITY DRILL (P2-03)
Usage:
  python3 tools/run_dr_drill.py
"""

import sys
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared.disaster_recovery_drill import DisasterRecoveryEngine


def main():
    engine = DisasterRecoveryEngine()
    print("Initiating PrimeNode ANTI Disaster Recovery & Integrity Drill...")
    report = engine.execute_full_dr_drill()

    print("\n================ DR DRILL CERTIFICATE ================")
    print(f"Drill ID:               {report.drill_id}")
    print(f"Status:                 {report.status}")
    print(f"Execution Duration:     {report.duration_ms} ms")
    print(f"WAL Integrity:          {json.dumps(report.wal_integrity, indent=2)}")
    print(f"Stale Leases Reclaimed: {report.leases_reclaimed}")
    print(f"Workers Recovered:      {report.workers_recovered}")
    print(f"Cold Reload Verified:   {report.cold_reload_verified}")
    print(f"Confirmed Revenue:      ${report.confirmed_revenue_usd} USDC")
    print(f"Pipeline Potential:     ${report.pipeline_potential_usd} USDC")
    print(f"Certificate SHA-256:    {report.certificate_sha256}")
    print("=======================================================\n")

    try:
        from shared.telegram_alert_bridge import get_telegram_notifier
        get_telegram_notifier().notify_dr_drill(
            drill_id=report.drill_id,
            status=report.status,
            duration_ms=report.duration_ms,
            cert_hash=report.certificate_sha256,
            wait=True
        )
    except Exception:
        pass

    if report.status == "RECOVERED_HEALTHY":
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
