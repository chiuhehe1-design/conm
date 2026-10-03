#!/usr/bin/env python3
"""
PRIME NODE VERSION & BUILD METADATA PROVIDER
Complies with P1.10:
Provides canonical build provenance without needing SSH access or guesswork.
Output Schema:
{
  "service": "anti-control-gateway",
  "version": "3.0.0",
  "git_sha": "eb2904b...",
  "build_id": "build-20261002-...",
  "deployed_at": "2026-10-02T...",
  "dirty": false
}
"""

import os
import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional

FALLBACK_BUILD_FILE = Path("/etc/anti-agents/build_info.json")
REPO_ROOT = Path(__file__).resolve().parent.parent


def get_version_metadata(service_name: str, base_dir: Optional[Path] = None) -> Dict[str, Any]:
    target_dir = base_dir or REPO_ROOT
    git_sha = "unknown"
    is_dirty = False
    version = "3.0.0"
    build_id = f"build-{datetime.now(timezone.utc).strftime('%Y%m%d')}-manual"
    deployed_at = datetime.now(timezone.utc).isoformat()

    # 1. Read pyproject.toml if present
    pyproject_file = target_dir / "pyproject.toml"
    if pyproject_file.exists():
        try:
            for line in pyproject_file.read_text().splitlines():
                if line.strip().startswith("version ="):
                    version = line.split("=")[1].strip().strip('"').strip("'")
                    break
        except Exception:
            pass

    # 2. Check Git metadata
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=2
        )
        if res.returncode == 0 and res.stdout.strip():
            git_sha = res.stdout.strip()

        diff_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=2
        )
        if diff_res.returncode == 0:
            is_dirty = bool(diff_res.stdout.strip())
    except Exception:
        pass

    # 3. Fallback to /etc/anti-agents/build_info.json if git not available or untracked
    if (git_sha == "unknown" or git_sha == "") and FALLBACK_BUILD_FILE.exists():
        try:
            info = json.loads(FALLBACK_BUILD_FILE.read_text())
            git_sha = info.get("git_sha", git_sha)
            is_dirty = info.get("dirty", is_dirty)
            build_id = info.get("build_id", build_id)
            deployed_at = info.get("deployed_at", deployed_at)
            version = info.get("version", version)
        except Exception:
            pass

    return {
        "service": service_name,
        "version": version,
        "git_sha": git_sha,
        "build_id": build_id,
        "deployed_at": deployed_at,
        "dirty": is_dirty
    }


if __name__ == "__main__":
    print(json.dumps(get_version_metadata("anti-agent-node"), indent=2))
