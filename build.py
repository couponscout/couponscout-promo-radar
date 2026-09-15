# ::ILANG
# [FILE:build.py] 职责: 读 data/offers.json 用 templates/ 渲染静态站到 site/
# [IN] data/offers.json + templates/ + .ilang/site.ilang  [OUT] site/
# [MUST] sitemap.xml robots.txt JSON-LD canonical 都由本文件生成 不许手写
# [RULE] 每页 canonical 指向自己 详情页嵌 schema.org Offer
# [BOUNDARY] never:给抓不到的字段编值|scope=permanent
# ::END

"""Static site generator. Reads the dataset, writes site/. Stdlib only."""

from __future__ import annotations

import html
import hashlib
import json
import re
import shutil
import string
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from ilang_config import (
    DEFAULT_CONFIG_PATH,
    get_categories,
    get_providers,
    get_render,
    get_settings,
    get_site,
    parse_ilang,
)

ROOT = Path(__file__).resolve().parent
TEMPLATES = ROOT / "templates"
DATA = ROOT / "data" / "offers.json"
SITE = ROOT / "site"

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "item"


def deal_slug(provider_name: str, offer: dict) -> str:
    """A stable, collision-free slug for one offer.

    Truncated titles collide constantly on a catalogue like ESR's, and colliding
    slugs silently overwrite each other's pages while the sitemap still lists
    both URLs. A short digest of the offer's identity keeps every page distinct
    and stable across rebuilds.
    """
    base = f"{slugify(provider_name)}-{slugify(str(offer.get('title', '')))[:60]}"
    key = f"{offer.get('title')}|{offer.get('price')}|{offer.get('offer_url')}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def render(template_name: str, **context) -> str:
    text = (TEMPLATES / template_name).read_text(encoding="utf-8")
    return string.Template(text).safe_substitute(**context)


def money(amount, currency) -> str:
    if amount is None:
        return ""
    cur = (currency or "USD").upper()
    symbols = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3", "JPY": "\u00a5"}
    symbol = symbols.get(cur)
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return ""
    body = f"{value:,.0f}" if cur == "JPY" or value.is_integer() and value >= 1000 else f"{value:,.2f}"
    return f"{symbol}{body}" if symbol else f"{body} {cur}"


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def pretty_date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return f"{MONTHS[dt.month - 1]} {dt.day}, {dt.year}"


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------
# page builders
# --------------------------------------------------------------------------

class Builder:
    def __init__(self, dataset: dict, cfg: dict):
        self.data = dataset
        self.cfg = cfg
        self.site = get_site(cfg)
        self.render_cfg = get_render(cfg)
        self.base = f"https://{self.site['domain']}"
        self.providers = dataset["providers"]
        self.generated_at = dataset["generated_at"]
        self.month = MONTHS[datetime.now(timezone.utc).month - 1]
        self.nav = [c.strip() for c in str(self.render_cfg.get("nav_order", "")).split(",") if c.strip()]
        self.categories = {c: [] for c in self.nav}
        for p in self.providers:
            self.categories.setdefault(p["category"], []).append(p)

    # -- shared chrome -----------------------------------------------------
    def head(self, *, title: str, description: str, canonical: str, jsonld: dict | None,
             og_type: str = "website", og_image_alt: str = "",
             og_image: str | None = None) -> str:
        blocks = ""
        if jsonld:
            blocks = (
                '<script type="application/ld+json">'
                + json.dumps(jsonld, ensure_ascii=False, separators=(",", ":"))
                + "</script>"
            )
        # Only advertise an image when the brand actually published one. An empty
        # og:image is worse than none: it makes the card render as a broken box.
        alt = esc(og_image_alt or title)
        if og_image:
            image_tags = (
                f'<meta property="og:image" content="{esc(og_image)}">\n'
                f'<meta property="og:image:alt" content="{alt}">\n'
                f'<meta name="twitter:image" content="{esc(og_image)}">'
            )
            og_card = "summary_large_image"
        else:
            image_tags = ""
            og_card = "summary"
        return render(
            "head.html",
            title=esc(title),
            description=esc(description),
            canonical=esc(canonical),
            site_title=esc(self.render_cfg["site_title"]),
            domain=esc(self.site["domain"]),
            og_type=esc(og_type),
            og_image_alt=alt,
            og_image_tags=image_tags,
            og_card=og_card,
            locale=esc(self.site["locale"]),
            jsonld=blocks,
        )

    def nav_html(self, active: str = "") -> str:
        items = [f'<a class="navlink{" active" if active == "all" else ""}" href="/">All deals</a>']
        for cat in self.nav:
            cls = "navlink active" if active == cat else "navlink"
            items.append(f'<a class="{cls}" href="/category/{slugify(cat)}.html">{esc(cat)}</a>')
        items.append(
            f'<a class="navlink{" active" if active == "compare" else ""}" href="/compare.html">Compare brands</a>'
        )
        return "".join(items)

    def shell(self, *, title, description, canonical, content, jsonld=None,
              active="", og_type="website", og_image_alt="", og_image=None) -> str:
        return render(
            "base.html",
            head=self.head(title=title, description=description, canonical=canonical,
                           jsonld=jsonld, og_type=og_type, og_image_alt=og_image_alt,
                           og_image=og_image),
            nav=self.nav_html(active),
            site_title=esc(self.render_cfg["site_title"]),
            tagline=esc(self.render_cfg["site_tagline"]),
            content=content,
            year=str(datetime.now(timezone.utc).year),
            generated=esc(pretty_date(self.generated_at)),
            generated_iso=esc(self.generated_at),
            domain=esc(self.site["domain"]),
            locale=esc(self.site["locale"]),
            lang=esc(str(self.site["locale"]).split("-")[0]),
        )

    # -- cards -------------------------------------------------------------
    def offer_card(self, offer: dict, provider: dict) -> str:
        price = money(offer.get("price"), offer.get("currency"))
        list_price = money(offer.get("list_price"), offer.get("currency"))
        pct = offer.get("discount_percent")
        deal_class = offer.get("deal_class") or ""

        # A percentage is only ever shown when a reference price existed.
        if pct:
            badge = f'<span class="badge">{pct:g}% off</span>'
        elif deal_class == "sale_page":
            badge = '<span class="badge badge-soft">On brand sale page</span>'
        elif deal_class == "listed":
            badge = '<span class="badge badge-soft">Listed</span>'
        else:
            badge = ""
        was = f'<span class="was">was {esc(list_price)}</span>' if list_price else ""
        # The brand's own product image. Hotlinked from their CDN, never copied or
        # re-hosted, and simply omitted when the source published none.
        image = offer.get("image")
        if image:
            img = (
                f'<img class="offer-img" src="{esc(image)}" alt="{esc(offer.get("title") or provider["name"])}" '
                f'loading="lazy" decoding="async" referrerpolicy="no-referrer">'
            )
        else:
            img = '<span class="offer-img offer-img-empty" aria-hidden="true"></span>'
        return render(
            "card_offer.html",
            url=f"/deal/{deal_slug(provider['name'], offer)}.html",
            img=img,
            title=esc(offer.get("title") or provider["name"]),
            provider=esc(provider["name"]),
            price=esc(price),
            was=was,
            badge=badge,
            class_slug=esc(deal_class.replace("_", "-")),
            fetched=esc(pretty_date(offer.get("fetched_at"))),
        )

    def provider_card(self, provider: dict) -> str:
        n = provider.get("offer_count", 0)
        verified = provider.get("verified_count", 0)
        on_sale = provider.get("sale_page_count", 0)
        if n:
            bits = []
            if verified:
                bits.append(f"{verified} verified discount{'s' if verified != 1 else ''}")
            if on_sale:
                bits.append(f"{on_sale} on the brand's sale page")
            meta = " &middot; ".join(bits) or f"{n} listing{'s' if n != 1 else ''}"
        else:
            meta = "Official promo page linked &middot; no machine-readable offer data"
        return render(
            "card_provider.html",
            url=f"/provider/{slugify(provider['name'])}.html",
            name=esc(provider["name"]),
            category=esc(provider["category"]),
            meta=meta,
        )

    # -- pages -------------------------------------------------------------
    def build_index(self) -> None:
        offers = [(p, o) for p in self.providers for o in p["offers"]]
        offers.sort(key=lambda po: po[1].get("discount_percent") or 0, reverse=True)
        top = offers[: int(self.render_cfg["items_per_page"])]

        sections = []
        for cat in self.nav:
            group = self.categories.get(cat) or []
            if not group:
                continue
            cards = "".join(self.provider_card(p) for p in group)
            sections.append(
                f'<section class="block"><h2>{esc(cat)}</h2><div class="grid">{cards}</div></section>'
            )

        deal_cards = "".join(self.offer_card(o, p) for p, o in top)
        stats = self.data["stats"]
        stat_bar = render(
            "statbar.html",
            brands=str(stats["providers_total"]),
            with_data=str(stats["providers_with_offers"]),
            offers=str(stats["offers_total"]),
            verified=str(stats.get("verified_discount_total", 0)),
            sale_page=str(stats.get("sale_page_total", 0)),
            generated=esc(pretty_date(self.generated_at)),
        )

        faq = [
            ("Where do these prices come from?",
             "Every price is read from the brand's own public product feed or from the "
             "schema.org Offer data on its official product page. Each listing links to the "
             "exact page it was read from, so you can check it yourself."),
            ("What is the difference between a verified discount and a sale-page listing?",
             "A verified discount means the brand's own page published a reference price, so we "
             "can compute the percentage - those are arithmetic on two published numbers. A "
             "sale-page listing means the item sits on a page the brand itself designates as a "
             "sale or promotion page; we show the real price but no percentage, because there "
             "was no reference price to compute one from."),
            ("How often is this updated?",
             "A scheduled job re-runs the scraper and rebuilds this site every 6 hours. "
             "If a discount disappears at the source, it disappears here."),
            ("Do you guarantee the price?",
             "No. A price is only accurate as of the timestamp shown on the listing. "
             "Always confirm on the brand's page before buying."),
            ("Why do some brands show no deals?",
             "Because their site publishes no machine-readable offer data. Rather than invent "
             "numbers, those brands get a page linking to their official promo page only."),
        ]
        faq_html = "".join(
            f'<details><summary>{esc(q)}</summary><p>{esc(a)}</p></details>' for q, a in faq
        )
        jsonld = {
            "@context": "https://schema.org",
            "@graph": [
                {
                    "@type": "ItemList",
                    "name": f"Live brand discounts - {self.month}",
                    "numberOfItems": len(top),
                    "itemListElement": [
                        {"@type": "ListItem", "position": i + 1,
                         "url": f"{self.base}/deal/{deal_slug(p['name'], o)}.html",
                         "name": f"{o.get('title')} - {p['name']}"}
                        for i, (p, o) in enumerate(top[:30])
                    ],
                },
                {
                    "@type": "FAQPage",
                    "mainEntity": [
                        {"@type": "Question", "name": q,
                         "acceptedAnswer": {"@type": "Answer", "text": a}}
                        for q, a in faq
                    ],
                },
            ],
        }
        content = render(
            "index.html",
            statbar=stat_bar,
            sections="".join(sections),
            deal_cards=deal_cards,
            deal_count=str(len(top)),
            month=esc(self.month),
            faq=faq_html,
            disclaimer=esc(
                "Prices are read from public brand sources and can change at any time. "
                "This site is independent and not affiliated with the brands listed."
            ),
        )
        title = f"{self.render_cfg['site_title']} - live brand discounts, updated {self.month}"
        hero_image = next((o.get("image") for _, o in offers if o.get("image")), None)
        write(SITE / "index.html",
              self.shell(title=title,
                         description=f"Verified promo pages and live discounts from "
                                     f"{stats['providers_total']} consumer brands across apparel, beauty, "
                                     f"tech accessories and home appliances. Rebuilt every 6 hours.",
                         canonical=f"{self.base}/", content=content, jsonld=jsonld, active="all",
                         og_image=hero_image))

    def build_providers(self) -> None:
        for provider in self.providers:
            self.build_provider(provider)
        for cat, group in self.categories.items():
            self.build_category(cat, group)

    def build_provider(self, provider: dict) -> None:
        name = provider["name"]
        offers = provider["offers"]
        cards = "".join(self.offer_card(o, provider) for o in offers)

        strategy_label = {
            "shopify_products_json": "the brand's public product feed",
            "shopify_collection_json": "the brand's own sale-collection feed",
            "sitemap_jsonld": "schema.org offer data on the brand's own product pages",
            "none": "none - this brand publishes no machine-readable offer data",
        }.get(provider["strategy"], provider["strategy"] or "none")

        if offers:
            low = min(o["price"] for o in offers)
            high = max(o["price"] for o in offers)
            cur = offers[0].get("currency") or self.site["currency"]
            offer_block = {
                "@type": "AggregateOffer",
                "priceCurrency": cur,
                "lowPrice": low,
                "highPrice": high,
                "offerCount": len(offers),
                "availability": "https://schema.org/InStock",
            }
            verified = provider.get("verified_count", 0)
            on_sale = provider.get("sale_page_count", 0)
            bits = []
            if verified:
                bits.append(f"{verified} verified discount{'s' if verified != 1 else ''}")
            if on_sale:
                bits.append(
                    f"{on_sale} listing{'s' if on_sale != 1 else ''} on the brand's own sale page"
                )
            summary = ", ".join(bits) or f"{len(offers)} live listing{'s' if len(offers) != 1 else ''}"
            status_note = f"{summary}, read from {strategy_label}."
        else:
            offer_block = None
            status_note = esc(provider["note"])

        graph = [
            {"@type": "BreadcrumbList", "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Home", "item": f"{self.base}/"},
                {"@type": "ListItem", "position": 2, "name": name,
                 "item": f"{self.base}/provider/{slugify(name)}.html"},
            ]},
            {"@type": "Organization", "name": name, "url": provider["homepage"]},
        ]
        if offer_block:
            product_block = {
                "@type": "Product",
                "name": f"{name} discounted products",
                "brand": {"@type": "Brand", "name": name},
                "url": f"{self.base}/provider/{slugify(name)}.html",
                "offers": offer_block,
            }
            lead_image = next((o.get("image") for o in offers if o.get("image")), None)
            if lead_image:
                product_block["image"] = lead_image
            graph.append(product_block)

        content = render(
            "provider.html",
            name=esc(name),
            category=esc(provider["category"]),
            homepage=esc(provider["homepage"]),
            promo=esc(provider["promo"]) if provider["promo"] else "",
            promo_row=(
                f'<li><span class="k">Official promo page</span>'
                f'<a class="v" href="{esc(provider["promo"])}" rel="noopener nofollow" target="_blank">'
                f'{esc(urlparse(provider["promo"]).netloc)}</a></li>'
                if provider["promo"] else ""
            ),
            strategy=esc(strategy_label),
            status=esc(provider["status"] or ""),
            status_note=status_note,
            cards=cards or '<p class="empty">No machine-readable offers right now.</p>',
            cards_heading=(f"Live deals from {name}" if offers
                           else f"{name} publishes no offer data - use the brand's own promo page"),
            fetched=esc(pretty_date(self.generated_at)),
            fetched_iso=esc(self.generated_at),
        )
        title = (f"{name} deals & promo page - {len(offers)} live deal"
                 f"{'s' if len(offers) != 1 else ''} ({self.month})") if offers else \
                f"{name} official promo page ({self.month})"
        write(SITE / "provider" / f"{slugify(name)}.html",
              self.shell(title=title,
                         description=(f"Live {name} discounts read from the brand's own public data. "
                                      f"{len(offers)} offer(s) tracked, refreshed every 6 hours.")
                                    if offers else
                                    (f"{name} publishes no machine-readable offer data. "
                                     f"Direct link to the official promo page instead."),
                         canonical=f"{self.base}/provider/{slugify(name)}.html",
                         content=content, jsonld={"@context": "https://schema.org", "@graph": graph},
                         active=provider["category"],
                         og_image=next((o.get("image") for o in offers if o.get("image")), None)))

    def build_category(self, cat: str, group: list[dict]) -> None:
        cards = "".join(self.provider_card(p) for p in group)
        total = sum(p.get("offer_count", 0) for p in group)
        content = render(
            "category.html",
            category=esc(cat),
            cards=cards,
            total=str(total),
            brands=str(len(group)),
            month=esc(self.month),
        )
        jsonld = {
            "@context": "https://schema.org",
            "@graph": [
                {"@type": "BreadcrumbList", "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "Home", "item": f"{self.base}/"},
                    {"@type": "ListItem", "position": 2, "name": cat,
                     "item": f"{self.base}/category/{slugify(cat)}.html"},
                ]},
                {"@type": "ItemList", "name": f"{cat} brand deals",
                 "numberOfItems": len(group),
                 "itemListElement": [
                     {"@type": "ListItem", "position": i + 1, "name": p["name"],
                      "url": f"{self.base}/provider/{slugify(p['name'])}.html"}
                     for i, p in enumerate(group)
                 ]},
            ],
        }
        write(SITE / "category" / f"{slugify(cat)}.html",
              self.shell(title=f"{cat} brand deals & promo pages ({self.month}) - {self.render_cfg['site_title']}",
                         description=f"{len(group)} {cat.lower()} brands tracked, {total} live discounts, "
                                     f"rebuilt every 6 hours from public brand data.",
                         canonical=f"{self.base}/category/{slugify(cat)}.html",
                         content=content, jsonld=jsonld, active=cat))

    def build_deals(self) -> None:
        index = []
        for provider in self.providers:
            for offer in provider["offers"]:
                slug = deal_slug(provider["name"], offer)
                self.build_deal(provider, offer, slug)
                index.append((slug, provider, offer))
        self.deal_index = index

    def build_deal(self, provider: dict, offer: dict, slug: str) -> None:
        price = money(offer.get("price"), offer.get("currency"))
        list_price = money(offer.get("list_price"), offer.get("currency"))
        pct = offer.get("discount_percent")
        deal_class = offer.get("deal_class") or ""
        url = f"{self.base}/deal/{slug}.html"

        if deal_class == "verified_discount":
            class_label = "Verified discount"
            class_note = (
                "The brand's own page published a reference price for this item, so the percentage "
                "above is arithmetic on two published numbers. Nothing here was estimated."
            )
        elif deal_class == "sale_page":
            class_label = "On the brand's sale page"
            class_note = (
                "This item sits on a page the brand itself designates as a sale or promotion page, and "
                "the price was read from that page. No discount percentage is shown because that page "
                "published no reference price to compute one from - we would rather show less than invent it."
            )
        else:
            class_label = ""
            class_note = ""

        product_node = {
            "@type": "Product",
            "name": offer.get("title") or provider["name"],
            "brand": {"@type": "Brand", "name": provider["name"]},
            "url": url,
            "offers": {
                "@type": "Offer",
                "price": offer["price"],
                "priceCurrency": offer.get("currency") or self.site["currency"],
                "availability": "https://schema.org/InStock",
                "url": offer["offer_url"],
                "seller": {"@type": "Organization", "name": provider["name"]},
            },
        }
        image = offer.get("image")
        if image:
            product_node["image"] = image

        jsonld = {
            "@context": "https://schema.org",
            "@graph": [
                {"@type": "BreadcrumbList", "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "Home", "item": f"{self.base}/"},
                    {"@type": "ListItem", "position": 2, "name": provider["name"],
                     "item": f"{self.base}/provider/{slugify(provider['name'])}.html"},
                    {"@type": "ListItem", "position": 3, "name": str(offer.get("title"))[:70], "item": url},
                ]},
                product_node,
            ],
        }
        media = (
            f'<div class="deal-media"><img src="{esc(image)}" '
            f'alt="{esc(offer.get("title") or provider["name"])}" '
            f'loading="eager" decoding="async" referrerpolicy="no-referrer"></div>'
            if image else ""
        )
        content = render(
            "deal.html",
            title=esc(offer.get("title") or provider["name"]),
            media=media,
            provider=esc(provider["name"]),
            provider_url=f"/provider/{slugify(provider['name'])}.html",
            category=esc(provider["category"]),
            price=esc(price) or "see brand page",
            list_price=esc(list_price),
            has_list_price="1" if list_price else "",
            pct=(f"{pct:g}%" if pct else ""),
            class_label=esc(class_label),
            class_slug=esc(deal_class.replace("_", "-")),
            class_note=esc(class_note),
            offer_url=esc(offer["offer_url"]),
            offer_host=esc(urlparse(offer["offer_url"]).netloc),
            source_url=esc(offer["source_url"]),
            source_host=esc(urlparse(offer["source_url"]).netloc),
            fetched=esc(pretty_date(offer.get("fetched_at"))),
            fetched_iso=esc(offer.get("fetched_at") or self.generated_at),
            valid_until_row=(
                f'<li><span class="k">Offer valid until</span><span class="v">'
                f'{esc(offer["valid_until"])}</span></li>'
                if offer.get("valid_until") else ""
            ),
            how=esc(
                f"Read automatically from {offer['source_url']} on "
                f"{pretty_date(offer.get('fetched_at'))}. No human edited this price."
            ),
        )
        title = (f"{offer.get('title')} - {pct:g}% off at {provider['name']} ({self.month})"
                 if pct else f"{offer.get('title')} at {provider['name']} ({self.month})")
        write(SITE / "deal" / f"{slug}.html",
              self.shell(title=title,
                         description=(f"{provider['name']}: {offer.get('title')} now {price}"
                                      + (f", was {list_price}" if list_price else "")
                                      + f". Price read from the brand's own public data on "
                                        f"{pretty_date(offer.get('fetched_at'))}."),
                         canonical=url, content=content, jsonld=jsonld,
                         active=provider["category"], og_type="product", og_image=image))

    def build_compare(self) -> None:
        rows = []
        for p in sorted(self.providers, key=lambda x: -x.get("offer_count", 0)):
            n = p.get("offer_count", 0)
            best = max((o.get("discount_percent") or 0) for o in p["offers"]) if n else 0
            low = min((o["price"] for o in p["offers"]), default=None)
            cur = (p["offers"][0].get("currency") if n else None) or self.site["currency"]
            rows.append(render(
                "row_compare.html",
                name=esc(p["name"]),
                url=f"/provider/{slugify(p['name'])}.html",
                category=esc(p["category"]),
                offers=str(n),
                best=f"{best:g}%" if best else "&mdash;",
                lowest=esc(money(low, cur)) if low is not None else "&mdash;",
                data=("public product feed" if p["strategy"] == "shopify_products_json"
                      else "brand sale-collection feed" if p["strategy"] == "shopify_collection_json"
                      else "schema.org Offer" if p["strategy"] == "sitemap_jsonld"
                      else "none found"),
                homepage=esc(p["homepage"]),
            ))
        content = render(
            "compare.html",
            rows="".join(rows),
            month=esc(self.month),
            fetched=esc(pretty_date(self.generated_at)),
        )
        jsonld = {
            "@context": "https://schema.org",
            "@graph": [
                {"@type": "BreadcrumbList", "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "Home", "item": f"{self.base}/"},
                    {"@type": "ListItem", "position": 2, "name": "Compare brands",
                     "item": f"{self.base}/compare.html"},
                ]},
                {"@type": "ItemList", "name": "Brands tracked",
                 "numberOfItems": len(self.providers),
                 "itemListElement": [
                     {"@type": "ListItem", "position": i + 1, "name": p["name"],
                      "url": f"{self.base}/provider/{slugify(p['name'])}.html"}
                     for i, p in enumerate(self.providers)
                 ]},
            ],
        }
        write(SITE / "compare.html",
              self.shell(title=f"Compare {len(self.providers)} brands - live discount count and lowest price",
                         description="Side-by-side view of every brand tracked: live discount count, "
                                     "best discount and lowest price, plus whether the brand publishes "
                                     "machine-readable offer data at all.",
                         canonical=f"{self.base}/compare.html", content=content,
                         jsonld=jsonld, active="compare"))

    def build_assets(self) -> None:
        (SITE / "assets").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TEMPLATES / "style.css", SITE / "assets" / "style.css")
        shutil.copyfile(TEMPLATES / "favicon.svg", SITE / "assets" / "favicon.svg")

    def build_sitemap(self) -> None:
        urls: list[tuple[str, str]] = [(f"{self.base}/", self.generated_at),
                                       (f"{self.base}/compare.html", self.generated_at)]
        for cat in self.categories:
            urls.append((f"{self.base}/category/{slugify(cat)}.html", self.generated_at))
        for p in self.providers:
            urls.append((f"{self.base}/provider/{slugify(p['name'])}.html", self.generated_at))
        for slug, _, offer in getattr(self, "deal_index", []):
            urls.append((f"{self.base}/deal/{slug}.html", offer.get("fetched_at") or self.generated_at))

        entries = "\n".join(
            f"  <url>\n    <loc>{esc(u)}</loc>\n    <lastmod>{esc(d)}</lastmod>\n"
            f"    <changefreq>daily</changefreq>\n  </url>" for u, d in urls
        )
        write(SITE / "sitemap.xml",
              '<?xml version="1.0" encoding="UTF-8"?>\n'
              '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
              f"{entries}\n</urlset>\n")
        write(SITE / "robots.txt",
              f"User-agent: *\nAllow: /\n\nSitemap: {self.base}/sitemap.xml\n")
        self.url_count = len(urls)

    def run(self) -> dict:
        """Rebuild site/ in place.

        Deliberately NOT `rmtree` + regenerate. The builder keeps a manifest of
        what it produced last time and removes only the files that dropped out of
        the new build. That keeps the output directory cheap to update in CI,
        avoids wiping anything a human might have put there, and makes the
        "these pages disappeared" case explicit instead of silent.
        """
        SITE.mkdir(parents=True, exist_ok=True)
        manifest_path = SITE / "_manifest.json"

        if manifest_path.exists():
            try:
                previous = set(json.loads(manifest_path.read_text(encoding="utf-8"))["files"])
            except Exception:
                previous = set()
        else:
            # First run under the manifest scheme: adopt whatever is already there
            # so pre-existing orphans get cleaned up rather than lingering forever.
            previous = {p.relative_to(SITE).as_posix() for p in SITE.rglob("*") if p.is_file()}

        self.build_assets()
        self.build_deals()
        self.build_index()
        self.build_providers()
        self.build_compare()
        self.build_sitemap()

        written = {p.relative_to(SITE).as_posix() for p in SITE.rglob("*") if p.is_file()}
        written.add("_manifest.json")

        # Hard self-check. Colliding slugs used to overwrite each other's pages
        # while the sitemap still listed both URLs, so the site shipped 404s.
        # Fail the build instead of shipping a sitemap that lies.
        expected = {f"deal/{slug}.html" for slug, _, _ in self.deal_index}
        offers_total = self.data["stats"]["offers_total"]
        if len(expected) != offers_total:
            raise RuntimeError(
                f"slug collision: {offers_total} offers but only {len(expected)} distinct deal pages"
            )
        missing = expected - written
        if missing:
            raise RuntimeError(
                f"{len(missing)} deal page(s) were never written, e.g. {sorted(missing)[:3]}"
            )
        sitemap = (SITE / "sitemap.xml").read_text(encoding="utf-8")
        for url in re.findall(r"<loc>(.*?)</loc>", sitemap):
            path = url.split(self.site["domain"], 1)[-1].lstrip("/") or "index.html"
            if path.endswith("/"):
                path += "index.html"
            if path not in written:
                raise RuntimeError(f"sitemap lists a page that was not built: {url}")

        # Retire pages this build no longer produces.
        removed = []
        for rel in sorted(previous - written):
            target = SITE / rel
            if target.is_file():
                target.unlink()
                removed.append(rel)
        for name in ("deal", "provider", "category", "assets"):
            folder = SITE / name
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()

        manifest_path.write_text(
            json.dumps(
                {"generated_at": self.generated_at, "files": sorted(written)},
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return {
            "pages": len(written) - 1,
            "providers": len(self.providers),
            "offers": offers_total,
            "retired": len(removed),
        }


def main() -> int:
    cfg = parse_ilang(DEFAULT_CONFIG_PATH)
    if not DATA.exists():
        print("data/offers.json missing - run scraper.py first")
        return 1
    dataset = json.loads(DATA.read_text(encoding="utf-8"))
    result = Builder(dataset, cfg).run()
    print(f"[build] {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
