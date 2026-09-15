# ::ILANG
# [FILE:scraper.py] 职责: 抓厂商公开的 feed / sitemap / 官方优惠页 写成 data/offers.json
# [IN] .ilang/site.ilang  [OUT] data/offers.json
# [RULE] 抓不到 price 就不写这条 不许估 不许编
# [RULE] 遵守 robots.txt 不绕反爬 不抓登录后内容
# [BOUNDARY] never:编优惠 编价格|scope:permanent
# ::END

"""Deterministic offer scraper. No LLM, no API keys, stdlib only.

Per provider, tried in order:

  A. Shopify public feed   <origin>/products.json              -> price + compare_at_price
  A2. Shopify collection    <origin>/collections/<x>/products.json -> the brand's own sale collection
  B. Sitemap + JSON-LD      product pages (promo page first)    -> schema.org Offer
  C. No structured source   provider page only, zero invented offers

Two classes of listing, and the site never blurs them:

  verified_discount  the page carried a reference price, so a real discount % was computed
  sale_page          read from a page the brand itself designates as a sale/promo page;
                     the real price is reported, no discount % is claimed

Whatever cannot be read is reported as such. Nothing is ever fabricated.
"""

from __future__ import annotations

import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path

from ilang_config import (
    DEFAULT_CONFIG_PATH,
    get_categories,
    get_fields,
    get_providers,
    get_render,
    get_settings,
    get_site,
    parse_ilang,
)

ROOT = Path(__file__).resolve().parent
OUT_PATH = ROOT / "data" / "offers.json"

# Fallback used only when .ilang/site.ilang does not define sale_path_hints
_DEFAULT_SALE_HINTS = (
    "/sale", "/sales", "/deals", "/deal", "/promotion", "/promotions", "/promo",
    "/offers", "/offer", "/clearance", "/outlet", "/discount",
    "/collections/sale", "/collections/deals", "/marketplace", "/trade-up",
)

# URLs that are definitely not a single product page
_EXCLUDE_HINTS = (
    "/collections/", "/collection/", "/pages/", "/page/", "/blogs/", "/blog/",
    "/support", "/help", "/about", "/careers", "/privacy", "/terms", "/legal",
    "/cart", "/account", "/search", "/login", "/register", "/newsletter",
    "/wishlist", "/store-locator", "/where-to-buy", "/sitemap", "/compare",
)
# URL shapes that usually ARE a single product page
_INCLUDE_HINTS = ("/p/", "/shop/p/", "/products/", "/product/", "/shop/")


def _sale_hints(settings: dict) -> tuple[str, ...]:
    raw = settings.get("sale_path_hints")
    if isinstance(raw, str) and raw.strip():
        return tuple(h.strip().lower() for h in raw.split(",") if h.strip())
    return _DEFAULT_SALE_HINTS


def _is_sale_scoped(url: str, settings: dict) -> bool:
    """True when the brand's own URL path says this page is a sale/promo page.

    This is the ONLY thing that may mark an offer as `sale_page`. It never
    invents a discount percentage; it only records that the brand filed the
    item under its own promotions section.
    """
    path = urllib.parse.urlparse(url).path.lower()
    if not path:
        return False
    return any(hint in path for hint in _sale_hints(settings))



# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _build_ssl_context() -> ssl.SSLContext:
    """Prefer certifi when present (some hosts need a fuller CA bundle)."""
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _build_ssl_context()


def http_get(url: str, settings: dict, *, accept_json: bool = False) -> tuple[int, str]:
    """GET a public URL. Returns (status, body). Raises on hard failure."""
    headers = {
        "User-Agent": settings["user_agent"],
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8" if accept_json else "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=settings["request_timeout"], context=SSL_CTX) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.status, raw.decode(charset, "ignore")


class RobotsGate:
    """Small robots.txt gate. Fail-open on network error, fail-closed on explicit rules."""

    def __init__(self, settings: dict):
        self.settings = settings
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _parser(self, origin: str):
        if origin in self._cache:
            return self._cache[origin]
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(urllib.parse.urljoin(origin, "/robots.txt"))
        try:
            status, body = http_get(rp.url, self.settings)
            rp.parse(body.splitlines())
        except Exception:
            rp = None
        self._cache[origin] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.settings.get("obey_robots", True):
            return True
        origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(url))
        rp = self._parser(origin)
        if rp is None:
            return True  # no robots.txt reachable -> public page, proceed
        try:
            return rp.can_fetch(self.settings["user_agent"], url)
        except Exception:
            return True


# --------------------------------------------------------------------------
# extractors
# --------------------------------------------------------------------------

def _as_list(node):
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def _walk_jsonld(node, out: list):
    if isinstance(node, dict):
        if node.get("@type") in ("Product", "Offer", "AggregateOffer"):
            out.append(node)
        for value in node.values():
            _walk_jsonld(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk_jsonld(value, out)


def _collect_offers(node, out: list) -> None:
    """Collect Offer/AggregateOffer nodes anywhere under `node` (no descent past one)."""
    if isinstance(node, dict):
        if node.get("@type") in ("Offer", "AggregateOffer"):
            out.append(node)
            return
        for value in node.values():
            _collect_offers(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_offers(value, out)


def _pairs(node, out: list) -> None:
    """Pair each Offer with the Product node that actually contains it.

    Listing pages carry many Product nodes. Attaching the first product's name to
    every offer on the page would mislabel the data, so each offer keeps its own
    parent product. Orphan offers are paired with None and resolved later.
    """
    if isinstance(node, dict):
        if node.get("@type") == "Product":
            offers: list = []
            _collect_offers(node, offers)
            for offer in offers:
                out.append((node, offer))
            return
        if node.get("@type") in ("Offer", "AggregateOffer"):
            out.append((None, node))
            return
        for value in node.values():
            _pairs(value, out)
    elif isinstance(node, list):
        for value in node:
            _pairs(value, out)


def _num(value):
    """Parse a price that may be str/int/float, tolerating currency symbols."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9.,]", "", str(value))
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif cleaned.count(",") == 1 and len(cleaned.split(",")[-1]) == 2:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _url_of(node) -> str | None:
    """Pull a URL out of a schema.org node, tolerating the @id / dict shapes."""
    if not isinstance(node, dict):
        return None
    for key in ("url", "mainEntityOfPage", "@id"):
        value = node.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
        if isinstance(value, dict):
            inner = value.get("@id")
            if isinstance(inner, str) and inner.startswith("http"):
                return inner
    return None


def _image_of(node) -> str | None:
    """First real image URL on a schema.org node. schema.org allows str, list or ImageObject."""
    if not isinstance(node, dict):
        return None
    for candidate in _as_list(node.get("image")):
        if isinstance(candidate, str) and candidate.startswith("http"):
            return candidate
        if isinstance(candidate, dict):
            inner = candidate.get("url") or candidate.get("contentUrl")
            if isinstance(inner, str) and inner.startswith("http"):
                return inner
    return None


def offers_from_jsonld(html: str, source_url: str, provider: str, fetched_at: str,
                       sale_scoped: bool = False) -> list[dict]:
    """Pull schema.org Product/Offer pairs out of a page.

    Each offer is labelled with the name of the product node it actually belongs
    to. Listing pages carry many products, and hanging the first product's name on
    every offer would mislabel the data. When an offer has no parent product and
    the page carries exactly one product we use it; otherwise we fall back to the
    brand name rather than guess which product it was.
    """
    blocks = re.findall(
        r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I
    )
    pairs: list = []
    products: list = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        try:
            parsed = json.loads(block)
        except Exception:
            continue
        _pairs(parsed, pairs)
        found: list = []
        _walk_jsonld(parsed, found)
        products.extend(n for n in found if n.get("@type") == "Product")

    named = [p.get("name") for p in products if p.get("name")]
    lone_product = named[0] if len(named) == 1 else None

    results: list[dict] = []
    for product, node in pairs:
        price = _num(node.get("price") or node.get("lowPrice"))
        if price is None:
            continue
        list_price = None
        for spec in _as_list(node.get("priceSpecification")):
            if isinstance(spec, dict) and "strikethrough" in str(spec.get("priceType", "")).lower():
                list_price = _num(spec.get("price"))
        title = (product or {}).get("name") or lone_product or provider
        url = _url_of(node) or _url_of(product) or source_url
        results.append({
            "provider": provider,
            "title": title,
            "price": price,
            "currency": node.get("priceCurrency"),
            "list_price": list_price,
            "image": _image_of(product) or _image_of(node),
            "offer_url": url,
            "valid_until": node.get("priceValidUntil"),
            "source_url": source_url,
            "fetched_at": fetched_at,
            "_sale_scoped": bool(sale_scoped),
        })
    return results


def offers_from_shopify(payload: dict, origin: str, source_url: str, provider: str,
                        fetched_at: str, settings: dict,
                        sale_scoped: bool = False) -> list[dict]:
    """Shopify /products.json -> one row per purchasable, discounted variant.

    With sale_scoped=True (a collection feed the brand itself named as a sale
    collection) variants without a compare_at_price are still kept, because the
    brand filed them under its own promotions. They are reported as real prices
    with no discount percentage, never as an invented one.
    """
    results: list[dict] = []
    for product in payload.get("products", []):
        handle = product.get("handle")
        product_url = f"{origin}/products/{handle}" if handle else source_url
        for variant in product.get("variants", []):
            price = _num(variant.get("price"))
            compare = _num(variant.get("compare_at_price"))
            if price is None:
                continue
            discounted = compare is not None and compare > price
            if not discounted and not sale_scoped:
                continue  # not actually discounted -> not a coupon
            if not variant.get("available", True):
                continue
            title = product.get("title") or provider
            variant_title = variant.get("title")
            # Many stores repeat the product title as the variant title; appending it
            # would produce "Name - Name" in every listing.
            if variant_title and variant_title not in ("Default Title", title):
                title = f"{title} - {variant_title}"
            images = product.get("images") or []
            image = images[0].get("src") if images and isinstance(images[0], dict) else None
            results.append({
                "provider": provider,
                "title": title,
                "price": price,
                "currency": settings.get("_currency", "USD"),
                "list_price": compare if discounted else None,
                "image": image if isinstance(image, str) and image.startswith("http") else None,
                "offer_url": product_url,
                "valid_until": None,
                "source_url": source_url,
                "fetched_at": fetched_at,
                "_sale_scoped": bool(sale_scoped),
            })
    return results


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def _origin(url: str) -> str:
    parts = urllib.parse.urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


def _collection_handle(url: str) -> str | None:
    """Return the collection handle if `url` looks like a Shopify /collections/<handle> page."""
    if not url:
        return None
    parts = urllib.parse.urlparse(url)
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) >= 2 and segments[-2].lower() == "collections":
        return segments[-1]
    return None


def _looks_like_product(url: str) -> bool:
    low = url.lower()
    if any(hint in low for hint in _EXCLUDE_HINTS):
        return False
    if any(hint in low for hint in _INCLUDE_HINTS):
        return True
    return low.endswith(".html")


def sitemap_candidates(origin: str, settings: dict, gate: RobotsGate) -> list[str]:
    """Collect product-looking URLs from a site's public sitemap(s)."""
    roots = ["/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml"]
    found: list[str] = []
    for root in roots:
        url = origin + root
        if not gate.allowed(url):
            continue
        try:
            status, body = http_get(url, settings)
        except Exception:
            continue
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", body, re.S | re.I)
        if not locs:
            continue
        children = [l for l in locs if l.lower().endswith(".xml")]
        if children:
            # fetch a bounded number of child sitemaps, preferring product-ish names
            children.sort(key=lambda u: 0 if "product" in u.lower() else 1)
            for child in children[:6]:
                if not gate.allowed(child):
                    continue
                try:
                    _, child_body = http_get(child, settings)
                except Exception:
                    continue
                found.extend(re.findall(r"<loc>\s*(.*?)\s*</loc>", child_body, re.S | re.I))
        else:
            found.extend(locs)
        if found:
            break

    seen, ordered = set(), []
    for url in found:
        if url in seen:
            continue
        seen.add(url)
        if _looks_like_product(url):
            ordered.append(url)
    return ordered


# --------------------------------------------------------------------------
# per-provider driver
# --------------------------------------------------------------------------

def scrape_provider(provider: dict, category: str, settings: dict, gate: RobotsGate,
                    fields: list[str]) -> dict:
    name = provider["name"]
    homepage = provider["homepage"]
    promo = provider["promo"]
    origin = _origin(homepage)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    record = {
        "name": name,
        "category": category,
        "homepage": homepage,
        "promo": promo,
        "affiliate": provider["affiliate"],
        "strategy": None,
        "status": None,
        "note": "",
        "offers": [],
    }

    # ---- Tier A: Shopify public feed -------------------------------------
    feed_url = f"{origin}/products.json?limit={int(settings['shopify_feed_limit'])}"
    if gate.allowed(feed_url):
        try:
            _, body = http_get(feed_url, settings, accept_json=True)
            payload = json.loads(body)
            if isinstance(payload, dict) and isinstance(payload.get("products"), list):
                settings["_currency"] = settings.get("_currency", "USD")
                offers = offers_from_shopify(payload, origin, feed_url, name, fetched_at, settings)
                if offers:
                    record.update(strategy="shopify_products_json", status="ok", offers=offers)
                    record["note"] = f"public product feed: {feed_url}"
                    return _finalise(record, settings, fields)
        except Exception as exc:  # feed missing / not JSON / blocked -> fall through
            record["note"] = f"shopify feed unavailable ({type(exc).__name__})"

    # ---- Tier A2: Shopify sale-collection feed ---------------------------
    # The brand itself filed these products under a sale collection, so every
    # row is on promotion by the brand's own definition.
    collection = _collection_handle(promo)
    if collection:
        coll_url = f"{origin}/collections/{collection}/products.json?limit={int(settings['shopify_feed_limit'])}"
        if gate.allowed(coll_url):
            try:
                _, body = http_get(coll_url, settings, accept_json=True)
                payload = json.loads(body)
                if isinstance(payload, dict) and isinstance(payload.get("products"), list):
                    settings["_currency"] = settings.get("_currency", "USD")
                    offers = offers_from_shopify(payload, origin, coll_url, name, fetched_at,
                                                 settings, sale_scoped=True)
                    if offers:
                        record.update(strategy="shopify_collection_json", status="ok", offers=offers)
                        record["note"] = (
                            f"the brand's own sale collection feed: {coll_url}"
                        )
                        return _finalise(record, settings, fields)
            except Exception:
                pass

    # ---- Tier B: sitemap + JSON-LD ---------------------------------------
    candidates = sitemap_candidates(origin, settings, gate)
    if promo and promo not in candidates:
        # The promo page is the highest-signal page a brand publishes: read it first.
        candidates = [promo] + candidates
    if not candidates:
        candidates = [homepage]

    offers: list[dict] = []
    pages_read = 0
    for url in candidates:
        if pages_read >= int(settings["max_pages_per_provider"]):
            break
        if not gate.allowed(url):
            continue
        try:
            _, html = http_get(url, settings)
        except Exception:
            continue
        pages_read += 1
        offers.extend(offers_from_jsonld(html, url, name, fetched_at,
                                         sale_scoped=_is_sale_scoped(url, settings)))
        if len(offers) >= int(settings["max_offers_per_provider"]):
            break
        time.sleep(float(settings["request_delay"]))

    if offers:
        record.update(
            strategy="sitemap_jsonld",
            status="ok",
            offers=offers[: int(settings["max_offers_per_provider"])],
        )
        record["note"] = f"schema.org Offer read from {pages_read} public product page(s)"
        return _finalise(record, settings, fields)

    # ---- Tier C: nothing structured -> honest empty provider page ---------
    record.update(strategy="none", status="no_structured_data")
    record["note"] = (
        "No public machine-readable offer data found on this site "
        "(no product feed, no schema.org Offer with a price). "
        "Only the official promo page is linked; no offer rows are invented."
    )
    return _finalise(record, settings, fields)


def _finalise(record: dict, settings: dict, fields: list[str]) -> dict:
    """Classify offers, drop the ones that are not a deal, dedupe, trim fields.

    An offer is kept when either:
      * a reference price was present and the discount clears the floor
        -> deal_class = verified_discount (a real percentage is published)
      * the page it was read from is a sale/promo page the brand itself
        designates, and allow_sale_page_listings is on
        -> deal_class = sale_page (real price, NO percentage claimed)
    """
    floor = float(settings["min_discount_percent"])
    allow_sale = bool(settings.get("allow_sale_page_listings", False))
    discount_only = bool(settings.get("discount_only", True))

    kept: list[dict] = []
    for offer in record["offers"]:
        price, list_price = offer.get("price"), offer.get("list_price")
        pct = None
        if price and list_price and list_price > price:
            pct = round((list_price - price) / list_price * 100, 1)

        sale_scoped = bool(offer.pop("_sale_scoped", False))

        if pct is not None and pct >= floor:
            offer["deal_class"] = "verified_discount"
            offer["discount_percent"] = pct
        elif allow_sale and sale_scoped:
            # Kept on the brand's own sale-page evidence only.
            offer["deal_class"] = "sale_page"
            offer.pop("discount_percent", None)   # never claim a percentage we cannot compute
            offer.pop("list_price", None)         # a reference price we could not verify
        elif not discount_only:
            # Operator explicitly turned the promotion requirement off.
            offer["deal_class"] = "listed"
            offer.pop("discount_percent", None)
            offer.pop("list_price", None)
        else:
            continue

        if offer.get("valid_until") is None:
            offer.pop("valid_until", None)
        if offer.get("list_price") is None:
            offer.pop("list_price", None)
        if offer.get("currency") is None:
            offer["currency"] = settings.get("_currency", "USD")
        kept.append({k: offer[k] for k in offer if k in fields or k == "provider"})

    # one row per distinct product at a distinct price on a distinct page
    seen: set[tuple] = set()
    unique: list[dict] = []
    for offer in kept:
        key = (offer.get("title"), offer.get("price"), offer.get("offer_url"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(offer)

    unique.sort(
        key=lambda o: (0 if o.get("deal_class") == "verified_discount" else 1,
                       -(o.get("discount_percent") or 0),
                       o.get("price") or 0)
    )
    record["offers"] = unique
    record["offer_count"] = len(unique)
    record["verified_count"] = sum(1 for o in unique if o.get("deal_class") == "verified_discount")
    record["sale_page_count"] = sum(1 for o in unique if o.get("deal_class") == "sale_page")

    if record["status"] == "ok" and not unique:
        record["status"] = "no_offers_after_filter"
        record["note"] = (
            f"Structured data was read but nothing met the {floor}% discount floor and no "
            "brand-designated sale page produced a listing. Nothing was invented to fill the page."
        )
    return record


def main() -> int:
    cfg = parse_ilang(DEFAULT_CONFIG_PATH)
    site = get_site(cfg)
    settings = get_settings(cfg)
    settings["_currency"] = site["currency"]
    providers = get_providers(cfg)
    categories = get_categories(cfg)
    fields = get_fields(cfg)
    render = get_render(cfg)

    if not providers:
        print("no providers in .ilang/site.ilang - nothing to do", file=sys.stderr)
        return 1

    gate = RobotsGate(settings)
    records = []
    for provider in providers:
        category = categories.get(provider["name"], "Other")
        print(f"[scrape] {provider['name']} ...", flush=True)
        try:
            record = scrape_provider(provider, category, settings, gate, fields)
        except Exception as exc:
            record = {
                "name": provider["name"],
                "category": category,
                "homepage": provider["homepage"],
                "promo": provider["promo"],
                "affiliate": provider["affiliate"],
                "strategy": "none",
                "status": "error",
                "note": f"{type(exc).__name__}: {exc}",
                "offers": [],
                "offer_count": 0,
            }
        print(
            f"          -> {record['status']} ({record.get('offer_count', 0)} offers, "
            f"strategy={record['strategy']})",
            flush=True,
        )
        records.append(record)
        time.sleep(float(settings["request_delay"]))

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "site": site,
        "render": render,
        "fields": fields,
        "providers": records,
        "stats": {
            "providers_total": len(records),
            "providers_with_offers": sum(1 for r in records if r.get("offer_count")),
            "offers_total": sum(r.get("offer_count", 0) for r in records),
            "verified_discount_total": sum(r.get("verified_count", 0) for r in records),
            "sale_page_total": sum(r.get("sale_page_count", 0) for r in records),
        },
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[scrape] wrote {OUT_PATH} :: {payload['stats']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
