"""Store adapters: detect the store from a URL, fetch the page, extract price/stock."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
TIMEOUT = 25

# domain -> store name
STORES = {
    "amazon.eg": "amazon",
    "noon.com": "noon",
    "jumia.com.eg": "jumia",
    "btech.com": "btech",
}
STORE_LABELS = {"amazon": "Amazon.eg", "noon": "Noon", "jumia": "Jumia", "btech": "B.TECH"}

BLOCK_MARKERS = (
    "captcha",
    "robot check",
    "Just a moment...",
    "Access Denied",
    "cf-challenge",
)


class FetchError(Exception):
    """Page could not be fetched or parsed."""


@dataclass
class Snapshot:
    title: str | None
    price: float | None
    currency: str
    in_stock: bool | None
    image: str | None


# ---------------------------------------------------------------- URL helpers

def detect_store(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    for domain, name in STORES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return None


_ASIN = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", re.I)
_NOON = re.compile(r"/(N[0-9A-Z]{8,}[A-Z]?)/p", re.I)
_JUMIA = re.compile(r"-(\d{6,})\.html")
_BTECH = re.compile(r"/p/([0-9a-f-]{36})", re.I)


def product_key(url: str, store: str) -> str:
    """Stable id for a product, used as the database key."""
    pattern = {"amazon": _ASIN, "noon": _NOON, "jumia": _JUMIA, "btech": _BTECH}.get(store)
    m = pattern.search(url) if pattern else None
    if m:
        ident = m.group(1).lower() if store == "btech" else m.group(1).upper()
    else:
        ident = hashlib.sha1(url.encode()).hexdigest()[:10]
    return f"{store}-{ident}"


def canonical_url(url: str, store: str) -> str:
    """Strip tracking params; force English for Amazon."""
    if store == "amazon":
        m = _ASIN.search(url)
        if m:
            return f"https://www.amazon.eg/dp/{m.group(1).upper()}?language=en_AE"
    p = urlparse(url)
    return f"{p.scheme or 'https'}://{p.netloc}{p.path}"


# ---------------------------------------------------------------- parsing

def parse_number(text) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(text))
    return float(m.group(0).replace(",", "")) if m else None


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _is_product(node: dict) -> bool:
    t = node.get("@type")
    return t == "Product" or (isinstance(t, list) and "Product" in t)


def parse_jsonld(soup: BeautifulSoup) -> Snapshot | None:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text(), strict=False)
        except (ValueError, TypeError):
            continue
        for node in _walk(data):
            if not (isinstance(node, dict) and _is_product(node)):
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if offers.get("@type") == "AggregateOffer":
                price = parse_number(offers.get("lowPrice") or offers.get("price"))
            else:
                price = parse_number(offers.get("price"))
            avail = str(offers.get("availability") or "")
            image = node.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            if isinstance(image, dict):
                image = image.get("url")
            return Snapshot(
                title=(node.get("name") or "").strip() or None,
                price=price,
                currency=offers.get("priceCurrency") or "EGP",
                in_stock=("InStock" in avail) if avail else (price is not None),
                image=image,
            )
    return None


def parse_meta(soup: BeautifulSoup) -> Snapshot | None:
    def meta(*names):
        for n in names:
            t = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if t and t.get("content"):
                return t["content"]
        return None

    price = parse_number(meta("product:price:amount", "og:price:amount"))
    if price is None:
        return None
    return Snapshot(
        title=meta("og:title"),
        price=price,
        currency=meta("product:price:currency", "og:price:currency") or "EGP",
        in_stock=True,
        image=meta("og:image"),
    )


AMAZON_PRICE_SELECTORS = (
    "#corePrice_feature_div .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-offscreen",
    "#apex_desktop .priceToPay .a-offscreen",
    "#apex_desktop .a-offscreen",
    "#price_inside_buybox",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
)


def parse_amazon(soup: BeautifulSoup) -> Snapshot:
    title_el = soup.select_one("#productTitle")
    title = title_el.get_text(strip=True) if title_el else None

    price = None
    for sel in AMAZON_PRICE_SELECTORS:
        el = soup.select_one(sel)
        if el and parse_number(el.get_text()):
            price = parse_number(el.get_text())
            break
    if price is None:  # split "whole" + "fraction" rendering
        whole = soup.select_one("#corePriceDisplay_desktop_feature_div .a-price-whole")
        if whole:
            frac = soup.select_one("#corePriceDisplay_desktop_feature_div .a-price-fraction")
            price = parse_number(whole.get_text() + "." + (frac.get_text() if frac else "0"))

    avail_el = soup.select_one("#availability")
    avail = avail_el.get_text(" ", strip=True).lower() if avail_el else ""
    if "unavailable" in avail or "out of stock" in avail or soup.select_one("#outOfStock"):
        in_stock = False
    elif price is not None:
        in_stock = True
    else:
        in_stock = None

    img = soup.select_one("#landingImage")
    image = (img.get("data-old-hires") or img.get("src")) if img else None
    return Snapshot(title=title, price=price, currency="EGP", in_stock=in_stock, image=image)


# ---------------------------------------------------------------- fetch

_session = requests.Session()
_session.headers.update(HEADERS)


def fetch(url: str, store: str) -> Snapshot:
    try:
        r = _session.get(url, timeout=TIMEOUT)
    except requests.Timeout:
        raise FetchError("timed out (the store may block automated requests)")
    except requests.RequestException as e:
        raise FetchError(f"network error: {e.__class__.__name__}")
    if r.status_code != 200:
        raise FetchError(f"HTTP {r.status_code}")

    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    if store == "amazon":
        if soup.select_one("form[action*='validateCaptcha']"):
            raise FetchError("blocked by Amazon captcha")
        snap = parse_amazon(soup)
        if snap.price is None and snap.in_stock is None:
            ld = parse_jsonld(soup)
            snap = ld or snap
    else:
        snap = parse_jsonld(soup) or parse_meta(soup)

    if snap is None or (snap.price is None and snap.in_stock is not False):
        lowered = html[:20000].lower()
        if any(m.lower() in lowered for m in BLOCK_MARKERS):
            raise FetchError("blocked by bot protection")
        raise FetchError("could not find a price on the page")
    return snap
