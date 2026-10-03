#!/usr/bin/env python3
"""
Test runner wrapper for external bounty crawlers unit tests.
Delegates to tests/test_external_bounty_crawlers.py.
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from tests.test_external_bounty_crawlers import *

if __name__ == "__main__":
    unittest.main()
