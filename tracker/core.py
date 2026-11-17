"""Adding products, checking prices, deciding alerts, keyword watches, Telegram commands."""
from __future__ import annotations

import html
import json
import os
import random
import re
import time
from datetime import timedelta

from . import db as dbm
from . import notify, search
from .stores import STORE_LABELS, FetchError, canonical_url, detect_store, fetch, product_key

URL_RE = re.compile(r"https?://\S+")
ERROR_ALERT_AFTER = 3          # consecutive failures before warning the user
HISTORY_HEARTBEAT = timedelta(hours=24)  # record a point at least daily even if unchanged
MAX_WATCH_ALERTS = 5           # per watch per run

Buttons = list[list[dict]]
Reply = tuple[str, Buttons | None]


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env) or default)
    except ValueError:
        return default


CHECK_INTERVAL = timedelta(hours=_f("CHECK_INTERVAL_HOURS", 3))
WATCH_INTERVAL = timedelta(hours=_f("WATCH_INTERVAL_HOURS", 6))
MIN_DROP_PCT = _f("MIN_DROP_PCT", 1.0)  # ignore drops smaller than this %


def fmt_price(p: float | None, cur: str = "EGP") -> str:
    if p is None:
        return "n/a"
    return f"{p:,.0f} {cur}" if p == int(p) else f"{p:,.2f} {cur}"


def short(title: str | None, n: int = 70) -> str:
    t = title or "(untitled)"
    return t if len(t) <= n else t[: n - 1] + "…"


def _due(last_iso: str | None, interval: timedelta) -> bool:
    if not last_iso:
        return True
    # small slack so an hourly cron doesn't skip a cycle because of a few seconds
    return dbm.parse_iso(dbm.now_iso()) - dbm.parse_iso(last_iso) >= interval - timedelta(minutes=10)


# ---------------------------------------------------------------- buttons

def product_buttons(p: dict) -> Buttons:
    s = p["sid"]
    return [
        [
            {"text": "🎯 Target", "callback_data": f"tg:{s}"},
            {"text": "📈 Chart", "callback_data": f"ch:{s}"},
            {"text": "💡 Cheaper?", "callback_data": f"al:{s}"},
            {"text": "🗑 Remove", "callback_data": f"rm:{s}"},
        ],
        [{"text": "🔗 Open product", "url": p["url"]}],
    ]


def track_buttons(db: dict, results: list[search.Result]) -> Buttons | None:
    row = []
    for i, r in enumerate(results, 1):
        if r.key in db["products"]:
            continue
        s = dbm.remember_candidate(db, r.to_dict())
        row.append({"text": f"➕ Track {i}", "callback_data": f"tr:{s}"})
    return [row[i:i + 3] for i in range(0, len(row), 3)] or None


def result_lines(db: dict, results: list[search.Result]) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        was = f" <s>{fmt_price(r.was_price)}</s>" if r.was_price else ""
        mine = " · ✅ tracking" if r.key in db["products"] else ""
        lines.append(
            f"{i}. <b>{fmt_price(r.price)}</b>{was} · {STORE_LABELS[r.store]}{mine}\n"
            f"    <a href=\"{html.escape(r.url)}\">{html.escape(short(r.title, 65))}</a>"
        )
    return "\n".join(lines)


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
        "sid": dbm.sid(key),
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

    if not first:
        if price is not None and prev_price and price < prev_price:
            pct = (prev_price - price) / prev_price * 100
            if pct >= MIN_DROP_PCT:
                low = " — lowest ever!" if p.get("lowest_price") and price < p["lowest_price"] else ""
                alerts.append(
                    f"📉 Price drop: {name}\n{fmt_price(prev_price, cur)} → <b>{fmt_price(price, cur)}</b> "
                    f"(−{pct:.1f}%){low}"
                )
        tgt = p.get("target_price")
        if tgt and price is not None and price <= tgt and (prev_price is None or prev_price > tgt):
            alerts.append(f"🎯 Target reached: {name}\nNow <b>{fmt_price(price, cur)}</b> (target {fmt_price(tgt, cur)})")
        if prev_stock is False and stock:
            alerts.append(f"✅ Back in stock: {name} at <b>{fmt_price(price, cur)}</b>")

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
    return force or _due(p.get("last_checked"), CHECK_INTERVAL)


def polite_pause() -> None:
    time.sleep(random.uniform(2.0, 5.0))


def alternatives_for(db: dict, key: str, p: dict) -> list[search.Result]:
    if not (p.get("title") and p.get("last_price")):
        return []
    alts = search.find_alternatives(p["title"], p["last_price"], key)
    p["alternatives"] = [r.to_dict() for r in alts]
    p["alternatives_at"] = dbm.now_iso()
    return alts


# ---------------------------------------------------------------- keyword watches

_RANGE = re.compile(r"\s+([\d,]+)\s*-\s*([\d,]+)\s*$")
_UNDER = re.compile(r"\s+(?:under|below|max|<=?|≤)\s*([\d,]+)\s*$", re.I)


def parse_watch(arg: str) -> tuple[str, float | None, float | None]:
    """'iphone 16 plus under 72000' / 'iphone 16 plus 60000-72000' -> (query, min, max)."""
    num = lambda s: float(s.replace(",", ""))
    s = " " + arg
    if m := _RANGE.search(s):
        return s[: m.start()].strip(), num(m.group(1)), num(m.group(2))
    if m := _UNDER.search(s):
        return s[: m.start()].strip(), None, num(m.group(1))
    return arg.strip(), None, None


def ordered_watches(db: dict) -> list[tuple[str, dict]]:
    return sorted(db["watches"].items(), key=lambda kv: kv[1].get("created_at", ""))


def resolve_watch(db: dict, ref: str) -> str | None:
    ref = ref.strip()
    if ref in db["watches"]:
        return ref
    items = ordered_watches(db)
    if ref.isdigit() and 0 < int(ref) <= len(items):
        return items[int(ref) - 1][0]
    return None


def watch_label(w: dict) -> str:
    rng = ""
    if w.get("min_price") and w.get("max_price"):
        rng = f" ({fmt_price(w['min_price'])}–{fmt_price(w['max_price'])})"
    elif w.get("max_price"):
        rng = f" (≤ {fmt_price(w['max_price'])})"
    return f"“{html.escape(w['query'])}”{rng}"


def watch_results(db: dict, w: dict) -> list[search.Result]:
    return search.find_matches(w["query"], w.get("min_price"), w.get("max_price"))


def create_watch(db: dict, arg: str) -> Reply:
    query, lo, hi = parse_watch(arg)
    if not search.parse_terms(query)[0]:
        return ("Usage: <code>/watch iphone 16 plus under 72000</code>\n"
                "or a range: <code>/watch ps5 slim 20000-25000</code>\n"
                "Exclude words with a minus: <code>/watch galaxy s25 -ultra</code>", None)
    wid = dbm.sid(f"{query}|{lo}|{hi}|{dbm.now_iso()}")
    w = {"query": query, "min_price": lo, "max_price": hi, "created_at": dbm.now_iso(),
         "last_run": dbm.now_iso(), "seen": {}}
    results = watch_results(db, w)
    w["seen"] = {r.key: r.price for r in results}
    db["watches"][wid] = w
    head = f"🔎 Watching {watch_label(w)} on Amazon.eg + B.TECH, every {WATCH_INTERVAL.total_seconds() / 3600:g} h."
    if not results:
        return head + "\nNo matches right now. I'll tell you when one appears.", None
    top = results[:MAX_WATCH_ALERTS]
    more = f"\n…and {len(results) - len(top)} more." if len(results) > len(top) else ""
    return (f"{head}\nCheapest matches now:\n{result_lines(db, top)}{more}\n\n"
            f"I'll alert you about new matches and lower prices."), track_buttons(db, top)


def run_watch(db: dict, w: dict) -> Reply | None:
    """Re-run a watch; return an alert only for new matches or lower prices."""
    results = watch_results(db, w)
    w["last_run"] = dbm.now_iso()
    seen = w.setdefault("seen", {})
    fresh = []
    for r in results:
        old = seen.get(r.key)
        if old is None or r.price < old:
            seen[r.key] = r.price
            if r.key not in db["products"]:
                fresh.append(r)
    if not fresh:
        return None
    top = fresh[:MAX_WATCH_ALERTS]
    return (f"🔎 New for {watch_label(w)}:\n{result_lines(db, top)}"), track_buttons(db, top)


# ---------------------------------------------------------------- Telegram commands

HELP = (
    "<b>Price tracker</b>\n"
    "Send a product link to track it (Amazon.eg, B.TECH; Jumia and Noon are best-effort).\n"
    "Optional target price after the link: <code>https://… 4500</code>\n\n"
    "/list — tracked products\n"
    "/target &lt;n&gt; &lt;price&gt; — set target price (0 clears)\n"
    "/chart &lt;n&gt; — price history chart\n"
    "/alternatives &lt;n&gt; — cheaper listings of the same model\n"
    "/remove &lt;n&gt; — stop tracking\n\n"
    "/watch iphone 16 plus under 72000 — alert me about any matching offer\n"
    "/watches — your watches · /unwatch &lt;n&gt;\n\n"
    "Prices are re-checked every 3 hours, watches every 6 hours."
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


def watches_text(db: dict) -> str:
    items = ordered_watches(db)
    if not items:
        return "No watches yet. Try <code>/watch iphone 16 plus under 72000</code>"
    return "\n".join(f"{i}. {watch_label(w)}" for i, (_, w) in enumerate(items, 1))


def add_from_url(db: dict, url: str, target: float | None) -> Reply:
    try:
        key, p = add_product(db, url, target)
    except AddError as e:
        return str(e), None
    already = p.get("last_checked") is not None
    if not already:
        check_product(p)
    name = html.escape(short(p.get("title") or p["url"]))
    if p.get("last_error"):
        return f"Added {name}, but couldn't read the price yet: {html.escape(p['last_error'])}", product_buttons(p)
    tgt = f"\nTarget: {fmt_price(p['target_price'])}" if p.get("target_price") else ""
    head = "Already tracking" if already else "Tracking ✅"
    text = f"{head} {name}\nCurrent: <b>{fmt_price(p['last_price'], p['currency'])}</b>{tgt}"
    buttons = product_buttons(p)
    if not already:
        alts = alternatives_for(db, key, p)
        if alts:
            text += f"\n\n💡 Cheaper listings of the same model:\n{result_lines(db, alts)}"
            buttons = (track_buttons(db, alts) or []) + buttons
    return text, buttons


def handle_message(db: dict, text: str) -> Reply:
    text = (text or "").strip()
    parts = text.split()
    cmd = parts[0].lower().split("@")[0] if parts else ""
    arg = text[len(parts[0]):].strip() if parts else ""

    if cmd in ("/start", "/help"):
        return HELP, None
    if cmd == "/list":
        return list_text(db), None
    if cmd == "/remove":
        key = dbm.resolve(db, parts[1]) if len(parts) > 1 else None
        if not key:
            return "Usage: /remove &lt;number from /list&gt;", None
        p = db["products"].pop(key)
        return f"🗑 Removed: {html.escape(short(p.get('title') or p['url']))}", None
    if cmd == "/target":
        key = dbm.resolve(db, parts[1]) if len(parts) > 2 else None
        if not key:
            return "Usage: /target &lt;number from /list&gt; &lt;price&gt;  (use 0 to clear)", None
        try:
            val = float(parts[2].replace(",", ""))
        except ValueError:
            return "Price must be a number.", None
        p = db["products"][key]
        p["target_price"] = val or None
        what = "cleared" if not val else f"set to <b>{fmt_price(val)}</b>"
        return f"🎯 Target {what} for {html.escape(short(p.get('title') or p['url'], 60))}", None
    if cmd == "/alternatives":
        key = dbm.resolve(db, parts[1]) if len(parts) > 1 else None
        if not key:
            return "Usage: /alternatives &lt;number from /list&gt;", None
        p = db["products"][key]
        alts = alternatives_for(db, key, p)
        name = html.escape(short(p.get("title") or p["url"], 60))
        if not alts:
            return f"No cheaper listings of {name} found on Amazon.eg or B.TECH right now.", None
        return f"💡 Cheaper than {fmt_price(p['last_price'])} for {name}:\n{result_lines(db, alts)}", track_buttons(db, alts)
    if cmd == "/track":
        cand = db["candidates"].get(parts[1]) if len(parts) > 1 else None
        if not cand:
            return "That result has expired. Send me the product link instead.", None
        return add_from_url(db, cand["url"], None)
    if cmd == "/watch":
        return create_watch(db, arg)
    if cmd == "/watches":
        return watches_text(db), None
    if cmd == "/unwatch":
        wid = resolve_watch(db, parts[1]) if len(parts) > 1 else None
        if not wid:
            return "Usage: /unwatch &lt;number from /watches&gt;", None
        w = db["watches"].pop(wid)
        return f"Stopped watching {watch_label(w)}", None

    m = URL_RE.search(text)
    if m:
        rest = text[m.end():].strip().replace(",", "")
        first = rest.split()[0] if rest else ""
        target = float(first) if re.fullmatch(r"\d+(\.\d+)?", first) else None
        return add_from_url(db, m.group(0), target)

    return HELP, None


INBOX = dbm.ROOT / "data" / "inbox"


def _inbox_order(path) -> int:
    return int(path.stem) if path.stem.isdigit() else 0


def _reply(db: dict, text: str, chat_id: str) -> None:
    try:
        body, buttons = handle_message(db, text)
    except Exception as e:  # never let one bad message block the queue
        print(f"[telegram] handling {text!r} failed: {e.__class__.__name__}: {e}")
        body, buttons = "⚠️ Something went wrong handling that. Please try again.", None
    notify.tg_send(body, chat_id, buttons)
    dbm.save(db)


def process_telegram(db: dict) -> None:
    allowed = notify.tg_allowed_chat()

    # Webhook mode: the Cloudflare Worker queues each message (and button tap, translated to a
    # text command) as data/inbox/<update_id>.json. Files are deleted in the working tree and the
    # deletion is committed at the end of the run, so if a run crashes they stay queued.
    for f in sorted(INBOX.glob("*.json"), key=_inbox_order):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            f.unlink()
            continue
        f.unlink()
        uid = int(m.get("update_id") or 0)
        if str(m.get("chat_id")) != allowed or uid < db["telegram_offset"]:
            continue  # stranger or already handled
        db["telegram_offset"] = uid + 1
        _reply(db, m.get("text", ""), str(m["chat_id"]))

    # Polling mode (no webhook set): read messages straight from Telegram.
    for upd in notify.tg_updates(db["telegram_offset"]):
        db["telegram_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not chat_id or chat_id != allowed:
            continue  # ignore strangers
        _reply(db, msg.get("text", ""), chat_id)


# ---------------------------------------------------------------- full run

def run(db: dict, force: bool = False) -> list[str]:
    process_telegram(db)
    dbm.save(db)

    sent: list[str] = []
    due = [(k, p) for k, p in dbm.ordered(db) if is_due(p, force)]
    print(f"Checking {len(due)} of {len(db['products'])} products")
    for i, (key, p) in enumerate(due):
        if i:
            polite_pause()
        alerts = check_product(p)
        status = p["last_error"] or fmt_price(p["last_price"], p["currency"])
        print(f"  [{p['store']}] {short(p.get('title') or p['url'], 60)} -> {status}")
        for a in alerts:
            notify.tg_send(a, buttons=product_buttons(p))
            sent.append(f'{a}\n<a href="{html.escape(p["url"])}">Open product</a>')
        dbm.save(db)

    due_w = [(k, w) for k, w in ordered_watches(db) if force or _due(w.get("last_run"), WATCH_INTERVAL)]
    print(f"Running {len(due_w)} of {len(db['watches'])} watches")
    for i, (wid, w) in enumerate(due_w):
        if i:
            polite_pause()
        alert = run_watch(db, w)
        print(f"  watch {w['query']!r} -> {'alert' if alert else 'nothing new'}")
        if alert:
            notify.tg_send(alert[0], buttons=alert[1])
            sent.append(alert[0])
        dbm.save(db)

    if sent:
        body = "<br><br>".join(a.replace("\n", "<br>") for a in sent)
        notify.email_send(f"Price tracker: {len(sent)} update(s)", body)
    return sent
