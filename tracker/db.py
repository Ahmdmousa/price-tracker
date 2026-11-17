"""Tiny JSON database committed to the repo (data/db.json)."""
from __future__ import annotations

import hashlib
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
    db.setdefault("watches", {})
    db.setdefault("candidates", {})  # sid -> search result offered with a "Track" button
    for key, p in db["products"].items():
        p.setdefault("sid", sid(key))
    return db


def sid(key: str) -> str:
    """Short stable id for Telegram buttons (callback_data is limited to 64 bytes)."""
    return hashlib.sha1(key.encode()).hexdigest()[:8]


def remember_candidate(db: dict, result: dict) -> str:
    """Store a search result so a "Track" button can refer to it by short id."""
    s = sid(result["key"])
    cands = db["candidates"]
    cands[s] = {"url": result["url"], "title": result["title"], "t": now_iso()}
    if len(cands) > 300:  # keep the newest 300
        for old in sorted(cands, key=lambda k: cands[k]["t"])[: len(cands) - 300]:
            del cands[old]
    return s


def save(db: dict) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, DB_PATH)


def ordered(db: dict) -> list[tuple[str, dict]]:
    """Products in the order they were added (used for /list numbering)."""
    return sorted(db["products"].items(), key=lambda kv: kv[1].get("added_at", ""))


def resolve(db: dict, ref: str) -> str | None:
    """Accept a list number (1-based), a product key, or a short id."""
    ref = ref.strip()
    if ref in db["products"]:
        return ref
    for key, p in db["products"].items():
        if p.get("sid") == ref:
            return key
    if ref.isdigit():
        items = ordered(db)
        i = int(ref) - 1
        if 0 <= i < len(items):
            return items[i][0]
    return None
