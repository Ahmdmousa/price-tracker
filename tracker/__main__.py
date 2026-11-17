"""CLI: python -m tracker <command>"""
from __future__ import annotations

import argparse
import sys

from . import core, dashboard, notify
from . import db as dbm


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="tracker", description="Personal price tracker")
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="process Telegram commands, check due products, alert, rebuild dashboard")
    r.add_argument("--force", action="store_true", help="check every product now, ignoring the interval")

    a = sub.add_parser("add", help="track a product URL")
    a.add_argument("url")
    a.add_argument("--target", type=float, help="alert when price <= target")

    rm = sub.add_parser("remove", help="stop tracking (number from `list` or key)")
    rm.add_argument("ref")

    sub.add_parser("list", help="show tracked products")
    sub.add_parser("dashboard", help="rebuild docs/index.html only")
    sub.add_parser("chat-id", help="print chat ids that messaged your bot (setup helper)")

    args = ap.parse_args(argv)
    db = dbm.load()

    if args.cmd in (None, "run"):
        core.run(db, force=getattr(args, "force", False))
        dbm.save(db)
        print("Dashboard:", dashboard.build(db))
    elif args.cmd == "add":
        try:
            key, p = core.add_product(db, args.url, args.target)
        except core.AddError as e:
            print(e)
            return 1
        core.check_product(p)
        dbm.save(db)
        dashboard.build(db)
        print(f"{key}: {p.get('title')} -> {p['last_error'] or core.fmt_price(p['last_price'], p['currency'])}")
    elif args.cmd == "remove":
        key = dbm.resolve(db, args.ref)
        if not key:
            print("Not found")
            return 1
        db["products"].pop(key)
        dbm.save(db)
        dashboard.build(db)
        print("Removed", key)
    elif args.cmd == "list":
        for i, (key, p) in enumerate(dbm.ordered(db), 1):
            status = p.get("last_error") or core.fmt_price(p.get("last_price"), p.get("currency", "EGP"))
            print(f"{i:>2}. [{p['store']}] {core.short(p.get('title') or p['url'], 60)} -> {status}  ({key})")
    elif args.cmd == "dashboard":
        print(dashboard.build(db))
    elif args.cmd == "chat-id":
        ups = notify.tg_updates(0)
        if not ups:
            print("No messages found. Send any message to your bot in Telegram, then run this again.")
        for u in ups:
            chat = (u.get("message") or {}).get("chat") or {}
            print(f"chat id: {chat.get('id')}  ({chat.get('first_name') or chat.get('title')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
