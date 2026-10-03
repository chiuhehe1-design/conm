#!/usr/bin/env python3
"""
MULTI-DATABASE POINT-IN-TIME SNAPSHOTTER & WAL INTEGRITY BACKUP (P3-02 / P3-18 / ANTI-017)
Backs up all SQLite databases using the ACID online backup API:
- revenue_portfolio.db
- workforce_scheduler.db
- tasks_ledger.db
- settlement.db
- autonomous_daemon.db
- financial_ledger.db

Generates a compressed tarball with SHA-256 manifest and prunes snapshots older than retention days.
"""

import os
import sys
import time
import json
import tarfile
import sqlite3
import hashlib
import tempfile
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("STATE_SNAPSHOTTER")

DEFAULT_DATA_DIR = Path(os.environ.get("ANTI_DATA_DIR", str(REPO_ROOT / "data")))
DEFAULT_BACKUP_DIR = Path(os.environ.get("ANTI_BACKUP_DIR", str(REPO_ROOT / "backups")))


def calculate_sha256(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class CompanyStateSnapshotter:
    """
    Online zero-downtime database backup engine.
    """

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        backup_dir: Optional[Path] = None,
        retention_days: int = 7
    ):
        self.data_dir = data_dir or DEFAULT_DATA_DIR
        self.backup_dir = backup_dir or DEFAULT_BACKUP_DIR
        self.retention_days = retention_days
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def create_snapshot(self) -> Dict[str, Any]:
        """
        Creates a point-in-time snapshot of all SQLite DB files in data_dir.
        """
        timestamp_str = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        snapshot_id = f"snapshot_{timestamp_str}"
        tar_filename = f"{snapshot_id}.tar.gz"
        dest_archive = self.backup_dir / tar_filename

        db_files = list(self.data_dir.glob("*.db"))
        backed_up_manifest: Dict[str, str] = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            for src_db in db_files:
                target_backup = tmppath / src_db.name
                # Perform atomic SQLite online backup
                try:
                    src_conn = sqlite3.connect(str(src_db))
                    dst_conn = sqlite3.connect(str(target_backup))
                    with dst_conn:
                        src_conn.backup(dst_conn)
                    src_conn.close()
                    dst_conn.close()

                    sha = calculate_sha256(target_backup)
                    backed_up_manifest[src_db.name] = sha
                except Exception as e:
                    logger.warning(f"Error during online backup of {src_db.name}: {e}")

            # Create manifest file
            manifest_file = tmppath / "manifest.json"
            manifest_data = {
                "snapshot_id": snapshot_id,
                "created_at": time.time(),
                "created_at_utc": time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()),
                "databases": backed_up_manifest,
                "total_databases": len(backed_up_manifest)
            }
            manifest_file.write_text(json.dumps(manifest_data, indent=2))

            # Package into tar.gz
            with tarfile.open(dest_archive, "w:gz") as tar:
                for item in tmppath.iterdir():
                    tar.add(item, arcname=item.name)

        archive_sha = calculate_sha256(dest_archive)
        size_bytes = dest_archive.stat().st_size

        # Prune old archives
        self._prune_old_snapshots()

        return {
            "snapshot_id": snapshot_id,
            "archive_path": str(dest_archive),
            "size_bytes": size_bytes,
            "archive_sha256": archive_sha,
            "manifest": manifest_data
        }

    def _prune_old_snapshots(self):
        cutoff = time.time() - (self.retention_days * 86400)
        for archive in self.backup_dir.glob("snapshot_*.tar.gz"):
            try:
                if archive.stat().st_mtime < cutoff:
                    archive.unlink()
                    logger.info(f"Pruned expired snapshot {archive.name}")
            except Exception as e:
                logger.warning(f"Failed to prune snapshot {archive.name}: {e}")


if __name__ == "__main__":
    import json
    snapshotter = CompanyStateSnapshotter()
    res = snapshotter.create_snapshot()
    print(json.dumps(res, indent=2))
