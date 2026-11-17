"""Adding products, checking prices, deciding alerts, handling Telegram commands."""
from __future__ import annotations

import html
import os
import random
import re
import time
from datetime import timedelta

from . import db as dbm
from . import notify
from .stores import STORE_LABELS, FetchError, canonical_url, detect_store, fetch, product_key

URL_RE = re.compile(r"https?://\S+")
ERROR_ALERT_AFTER = 3          # consecutive failures before warning the user
HISTORY_HEARTBEAT = timedelta(hours=24)  # record a point at least daily even if unchanged


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except ValueError:
        return default


CHECK_INTERVAL = timedelta(hours=_f("CHECK_INTERVAL_HOURS", 3))
MIN_DROP_PCT = _f("MIN_DROP_PCT", 1.0)  # ignore drops smaller than this %


def fmt_price(p: float | None, cur: str = "EGP") -> str:
    if p is None:
        return "n/a"
    return f"{p:,.0f} {cur}" if p == int(p) else f"{p:,.2f} {cur}"


def short(title: str | None, n: int = 70) -> str:
    t = title or "(untitled)"
    return t if len(t) <= n else t[: n - 1] + "…"


# ---------------------------------------------------------------- products

class AddError(Exception):
    pass


def add_product(db: dict, url: str, target: float | None = None) -> tuple[str, dict]:
    store = detect_store(url)
    if not store:
        raise AddError("Unsupported store. Supported: amazon.eg, noon.com, jumia.com.eg, btech.com")
    key = product_key(url, store)
    if key in db["products"]:
        p = db["products"][key]
        if target is not None:
            p["target_price"] = target
        return key, p
    p = {
        "url": canonical_url(url, store),
        "store": store,
        "title": None,
        "image": None,
        "currency": "EGP",
        "target_price": target,
        "added_at": dbm.now_iso(),
        "last_checked": None,
        "last_price": None,
        "last_in_stock": None,
        "lowest_price": None,
        "highest_price": None,
        "last_error": None,
        "error_count": 0,
        "history": [],
    }
    db["products"][key] = p
    return key, p


def check_product(p: dict) -> list[str]:
    """Fetch one product, update it in place, return alert messages (HTML)."""
    alerts: list[str] = []
    p["last_checked"] = dbm.now_iso()
    try:
        snap = fetch(p["url"], p["store"])
    except FetchError as e:
        p["last_error"] = str(e)
        p["error_count"] = p.get("error_count", 0) + 1
        if p["error_count"] == ERROR_ALERT_AFTER:
            alerts.append(
                f"⚠️ Can't read the price for <b>{html.escape(short(p['title'] or p['url']))}</b> "
                f"({STORE_LABELS[p['store']]}) after {ERROR_ALERT_AFTER} tries: {html.escape(str(e))}"
            )
        return alerts

    prev_price, prev_stock = p.get("last_price"), p.get("last_in_stock")
    first = prev_price is None and prev_stock is None
    p["error_count"], p["last_error"] = 0, None
    p["title"] = snap.title or p.get("title")
    p["image"] = snap.image or p.get("image")
    p["currency"] = snap.currency or p.get("currency") or "EGP"

    price, stock = snap.price, snap.in_stock
    cur = p["currency"]
    name = f"<b>{html.escape(short(p['title']))}</b> ({STORE_LABELS[p['store']]})"
    link = f'\n<a href="{html.escape(p["url"])}">Open product</a>'

    if not first:
        if price is not None and prev_price and price < prev_price:
            pct = (prev_price - price) / prev_price * 100
            if pct >= MIN_DROP_PCT:
                low = " — lowest ever!" if p.get("lowest_price") and price < p["lowest_price"] else ""
                alerts.append(
                    f"📉 Price drop: {name}\n{fmt_price(prev_price, cur)} → <b>{fmt_price(price, cur)}</b> "
                    f"(−{pct:.1f}%){low}{link}"
                )
        tgt = p.get("target_price")
        if tgt and price is not None and price <= tgt and (prev_price is None or prev_price > tgt):
            alerts.append(f"🎯 Target reached: {name}\nNow <b>{fmt_price(price, cur)}</b> (target {fmt_price(tgt, cur)}){link}")
        if prev_stock is False and stock:
            alerts.append(f"✅ Back in stock: {name} at <b>{fmt_price(price, cur)}</b>{link}")

    # history: record on change, or at least once a day
    hist = p.setdefault("history", [])
    last = hist[-1] if hist else None
    changed = not last or last.get("p") != price or last.get("s") != stock
    stale = last and dbm.parse_iso(p["last_checked"]) - dbm.parse_iso(last["t"]) >= HISTORY_HEARTBEAT
    if changed or stale:
        hist.append({"t": p["last_checked"], "p": price, "s": stock})

    p["last_price"], p["last_in_stock"] = price, stock
    if price is not None:
        p["lowest_price"] = min(price, p["lowest_price"]) if p.get("lowest_price") else price
        p["highest_price"] = max(price, p["highest_price"]) if p.get("highest_price") else price
    return alerts


def is_due(p: dict, force: bool = False) -> bool:
    if force or not p.get("last_checked"):
        return True
    last = dbm.parse_iso(p["last_checked"])
    # small slack so an hourly cron doesn't skip a cycle because of a few seconds
    return dbm.parse_iso(dbm.now_iso()) - last >= CHECK_INTERVAL - timedelta(minutes=10)


def polite_pause() -> None:
    time.sleep(random.uniform(2.0, 5.0))


# ---------------------------------------------------------------- Telegram commands

HELP = (
    "<b>Price tracker</b>\n"
    "Send a product link to track it (Amazon.eg, Jumia, B.TECH, Noon).\n"
    "Optional target price after the link: <code>https://… 4500</code>\n\n"
    "/list — tracked products\n"
    "/target &lt;n&gt; &lt;price&gt; — set target price\n"
    "/remove &lt;n&gt; — stop tracking\n"
    "/help — this message\n\n"
    "Commands are processed on the next scheduled run (up to ~1 hour)."
)


def list_text(db: dict) -> str:
    items = dbm.ordered(db)
    if not items:
        return "Nothing tracked yet. Send me a product link."
    lines = []
    for i, (_, p) in enumerate(items, 1):
        price = fmt_price(p.get("last_price"), p.get("currency", "EGP"))
        tgt = f" · target {fmt_price(p['target_price'])}" if p.get("target_price") else ""
        stock = " · out of stock" if p.get("last_in_stock") is False else ""
        err = " · ⚠️ error" if p.get("error_count") else ""
        lines.append(
            f"{i}. <a href=\"{html.escape(p['url'])}\">{html.escape(short(p.get('title') or p['url'], 55))}</a>\n"
            f"    {STORE_LABELS[p['store']]} · <b>{price}</b>{tgt}{stock}{err}"
        )
    return "\n".join(lines)


def handle_message(db: dict, text: str) -> str:
    text = (text or "").strip()
    cmd = text.split()[0].lower().split("@")[0] if text else ""

    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/list":
        return list_text(db)
    if cmd == "/remove":
        parts = text.split()
        key = dbm.resolve(db, parts[1]) if len(parts) > 1 else None
        if not key:
            return "Usage: /remove &lt;number from /list&gt;"
        p = db["products"].pop(key)
        return f"Removed: {html.escape(short(p.get('title') or p['url']))}"
    if cmd == "/target":
        parts = text.split()
        key = dbm.resolve(db, parts[1]) if len(parts) > 2 else None
        if not key:
            return "Usage: /target &lt;number from /list&gt; &lt;price&gt;  (use 0 to clear)"
        try:
            val = float(parts[2].replace(",", ""))
        except ValueError:
            return "Price must be a number."
        db["products"][key]["target_price"] = val or None
        return f"Target {'cleared' if not val else 'set to ' + fmt_price(val)} for #{parts[1]}."

    m = URL_RE.search(text)
    if m:
        url = m.group(0)
        rest = text[m.end():].strip().replace(",", "")
        target = float(rest.split()[0]) if rest and re.fullmatch(r"\d+(\.\d+)?", rest.split()[0]) else None
        try:
            key, p = add_product(db, url, target)
        except AddError as e:
            return str(e)
        if p.get("last_checked") is None:
            check_product(p)
        name = html.escape(short(p.get("title") or p["url"]))
        if p.get("last_error"):
            return f"Added {name}, but couldn't read the price yet: {html.escape(p['last_error'])}"
        tgt = f"\nTarget: {fmt_price(p['target_price'])}" if p.get("target_price") else ""
        return f"Tracking ✅ {name}\nCurrent: <b>{fmt_price(p['last_price'], p['currency'])}</b>{tgt}"

    return HELP


def process_telegram(db: dict) -> None:
    allowed = notify.tg_allowed_chat()
    for upd in notify.tg_updates(db["telegram_offset"]):
        db["telegram_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not chat_id or chat_id != allowed:
            continue  # ignore strangers
        reply = handle_message(db, msg.get("text", ""))
        notify.tg_send(reply, chat_id)


# ---------------------------------------------------------------- full run

def run(db: dict, force: bool = False) -> list[str]:
    process_telegram(db)
    dbm.save(db)

    all_alerts: list[str] = []
    due = [p for _, p in dbm.ordered(db) if is_due(p, force)]
    print(f"Checking {len(due)} of {len(db['products'])} products")
    for i, p in enumerate(due):
        if i:
            polite_pause()
        alerts = check_product(p)
        status = p["last_error"] or fmt_price(p["last_price"], p["currency"])
        print(f"  [{p['store']}] {short(p.get('title') or p['url'], 60)} -> {status}")
        all_alerts.extend(alerts)
        dbm.save(db)

    for a in all_alerts:
        notify.tg_send(a)
    if all_alerts:
        body = "<br><br>".join(a.replace("\n", "<br>") for a in all_alerts)
        notify.email_send(f"Price tracker: {len(all_alerts)} update(s)", body)
    return all_alerts
