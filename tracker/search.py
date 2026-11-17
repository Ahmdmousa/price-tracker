"""Store search (Amazon.eg, B.TECH) and matching, used by keyword watches and alternatives."""
from __future__ import annotations

import json
import random
import re
import time
from dataclasses import asdict, dataclass
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from .stores import _session, parse_number, product_url

ACCESSORY_WORDS = {
    "case", "cover", "glass", "protector", "charger", "cable", "adapter", "strap", "skin",
    "holder", "stand", "sticker", "film", "lens", "bumper", "pouch", "sleeve",
}
# Model words: a result that has one of these when the query doesn't is a different model
# (e.g. "iphone 16" should not match "iphone 16 pro").
MODEL_MODIFIERS = {"pro", "max", "plus", "ultra", "mini", "lite", "fe", "air", "neo", "prime", "edge"}
STOPWORDS = {
    "with", "and", "for", "the", "dual", "single", "sim", "ram", "storage", "memory", "phone",
    "smartphone", "mobile", "face", "id", "new", "version", "international", "tax", "paid",
    "wifi", "wi", "fi", "nano", "esim", "e", "of", "in", "by", "a",
}
SPEC_RE = re.compile(r"^(\d+(?:\.\d+)?(gb|tb|mb)|[345]g|\d+(mah|hz|w|mp|inch|in|cm|mm))$")


@dataclass
class Result:
    store: str
    ident: str
    key: str
    title: str
    price: float
    was_price: float | None
    in_stock: bool
    url: str
    image: str | None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- text helpers

def normalize(text: str) -> list[str]:
    """Lowercase tokens; "256 GB" -> "256gb", punctuation dropped."""
    t = (text or "").lower()
    t = re.sub(r"(\d+(?:\.\d+)?)\s*(gb|tb|mb)\b", r"\1\2", t)
    t = re.sub(r"[^\w.]+", " ", t)
    return [w.strip(".") for w in t.split() if w.strip(".")]


def _capacity_gb(tok: str) -> float | None:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(gb|tb)", tok)
    if not m:
        return None
    return float(m.group(1)) * (1024 if m.group(2) == "tb" else 1)


def model_terms(title: str) -> list[str]:
    """Search terms for "the same model" as a product title.

    Brand/model words up to the first spec token (max 5), plus the largest storage
    capacity, e.g. "Apple iPhone 16 Plus (256 GB) - Ultramarine with Face ID"
    -> ["apple", "iphone", "16", "plus", "256gb"].
    """
    head = re.split(r"\s[-|–]\s|\|", title or "", maxsplit=1)[0]
    toks = normalize(head)
    core: list[str] = []
    for t in toks:
        if SPEC_RE.match(t):
            break
        if t in STOPWORDS:
            continue
        core.append(t)
        if len(core) == 5:
            break
    caps = [(c, t) for t in normalize(title) if (c := _capacity_gb(t))]
    if caps:
        core.append(max(caps)[1])
    return core


def parse_terms(query: str) -> tuple[list[str], set[str]]:
    """Split a user query into required terms and "-excluded" words."""
    req, excl = [], set()
    for raw in query.split():
        if raw.startswith("-") and len(raw) > 1:
            excl.update(normalize(raw[1:]))
        else:
            req.extend(normalize(raw))
    return req, excl


def matches(title: str, terms: list[str], exclude: set[str] = frozenset(),
            allow_missing: int = 0) -> bool:
    toks = set(normalize(title))
    missing = [t for t in terms if t not in toks]
    # Only plain words (e.g. a brand name one store omits) may be missing. Model numbers,
    # storage sizes ("16", "a56", "256gb") and model words ("plus", "pro") never are:
    # 128GB / iPhone 16 are different products from 256GB / iPhone 16 Plus.
    essential = [t for t in missing if any(ch.isdigit() for ch in t) or t in MODEL_MODIFIERS]
    if len(missing) > allow_missing or essential:
        return False
    term_set = set(terms)
    if toks & (exclude | (ACCESSORY_WORDS - term_set)):
        return False
    if toks & (MODEL_MODIFIERS - term_set):
        return False
    return True


# ---------------------------------------------------------------- stores

_SPONSORED = re.compile(r"^\s*sponsored\s*(ad)?\s*[–\-:]?\s*", re.I)


def _amazon_title(item) -> str:
    """Amazon renders the title in different places depending on layout (sometimes the
    h2 only holds the brand). The product image's alt text is the cleanest source;
    fall back to the longest h2 text."""
    img = item.select_one("img.s-image")
    alt = (img.get("alt") or "").strip() if img else ""
    if len(alt) >= 15:
        best = alt
    else:
        cands = [h.get("aria-label") or h.get_text(" ", strip=True) for h in item.select("h2")]
        best = max(cands, key=len, default="")
    return _SPONSORED.sub("", re.sub(r"\s+", " ", best)).strip()


def search_amazon(query: str) -> list[Result]:
    r = _session.get(f"https://www.amazon.eg/s?k={quote_plus(query)}&language=en_AE", timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()
    for it in soup.select('div[data-component-type="s-search-result"]'):
        asin = (it.get("data-asin") or "").upper()
        price_el = it.select_one(".a-price:not(.a-text-price) .a-offscreen")
        title = _amazon_title(it)
        if not (asin and title and price_el) or asin in seen:
            continue
        seen.add(asin)
        price = parse_number(price_el.get_text())
        if not price:
            continue
        was_el = it.select_one(".a-price.a-text-price .a-offscreen")
        img = it.select_one("img.s-image")
        out.append(Result(
            store="amazon", ident=asin, key=f"amazon-{asin}",
            title=title,
            price=price, was_price=parse_number(was_el.get_text()) if was_el else None,
            in_stock=True, url=product_url("amazon", asin), image=img.get("src") if img else None,
        ))
    return out


_BTECH_ITEM = re.compile(r'\{"variant_id":"')


def search_btech(query: str) -> list[Result]:
    r = _session.get(f"https://btech.com/en/s?q={quote_plus(query)}", timeout=25)
    r.raise_for_status()
    text = r.text.replace('\\"', '"')
    dec = json.JSONDecoder()
    out, seen = [], set()
    for m in _BTECH_ITEM.finditer(text):
        try:
            item, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        slug = (item.get("slug") or "").lower()
        price = parse_number((item.get("price") or {}).get("final_price"))
        if not slug or not price or slug in seen:
            continue
        seen.add(slug)
        base = parse_number((item.get("price") or {}).get("base_price"))
        out.append(Result(
            store="btech", ident=slug, key=f"btech-{slug}", title=item.get("name") or "",
            price=price, was_price=base if base and base > price else None,
            in_stock=bool(item.get("is_in_stock", True)), url=product_url("btech", slug),
            image=None,
        ))
    return out


SEARCHERS = {"amazon": search_amazon, "btech": search_btech}


def search_all(query: str) -> list[Result]:
    results: list[Result] = []
    for i, (name, fn) in enumerate(SEARCHERS.items()):
        if i:
            time.sleep(random.uniform(1.5, 3.0))
        try:
            results.extend(fn(query))
        except Exception as e:  # one store failing must not break the other
            print(f"[search] {name} failed for {query!r}: {e.__class__.__name__}: {e}")
    return results


# ---------------------------------------------------------------- features

def find_matches(query: str, min_price: float | None = None, max_price: float | None = None) -> list[Result]:
    """Keyword watch: every term required, accessories/other models excluded, price range."""
    terms, excl = parse_terms(query)
    if not terms:
        return []
    best: dict[str, Result] = {}
    for r in sorted(search_all(" ".join(terms)), key=lambda r: r.price):
        if (r.in_stock and matches(r.title, terms, excl)
                and (min_price is None or r.price >= min_price)
                and (max_price is None or r.price <= max_price)):
            best.setdefault(r.key, r)
    return list(best.values())


def find_alternatives(title: str, price: float | None, own_key: str, limit: int = 3) -> list[Result]:
    """Cheaper listings of the same model (other colours, sellers, stores)."""
    terms = model_terms(title)
    if len(terms) < 2 or not price:
        return []
    allow = 1 if len(terms) >= 5 else 0
    out = [
        r for r in search_all(" ".join(terms))
        if r.key != own_key and r.in_stock and r.price < price and r.price >= price * 0.5
        and matches(r.title, terms, allow_missing=allow)
    ]
    best: dict[str, Result] = {}
    for r in sorted(out, key=lambda r: r.price):
        best.setdefault(r.key, r)
    return list(best.values())[:limit]
