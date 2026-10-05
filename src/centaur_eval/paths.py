"""Repository paths and content hashing, defined once."""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = ROOT / "prompts"
DATA_DIR = ROOT / "data"
SCENARIO_DIR = DATA_DIR / "scenarios"
RUNS_DIR = ROOT / "runs"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]
