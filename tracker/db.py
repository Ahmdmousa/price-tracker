"""Tiny JSON database committed to the repo (data/db.json)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "db.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def load() -> dict:
    if DB_PATH.exists():
        db = json.loads(DB_PATH.read_text(encoding="utf-8"))
    else:
        db = {}
    db.setdefault("telegram_offset", 0)
    db.setdefault("products", {})
    return db


def save(db: dict) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, DB_PATH)


def ordered(db: dict) -> list[tuple[str, dict]]:
    """Products in the order they were added (used for /list numbering)."""
    return sorted(db["products"].items(), key=lambda kv: kv[1].get("added_at", ""))


def resolve(db: dict, ref: str) -> str | None:
    """Accept a list number (1-based) or a product key."""
    ref = ref.strip()
    if ref in db["products"]:
        return ref
    if ref.isdigit():
        items = ordered(db)
        i = int(ref) - 1
        if 0 <= i < len(items):
            return items[i][0]
    return None
