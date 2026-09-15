# ::ILANG
# [FILE:ilang_config.py] 职责: 解析 .ilang/site.ilang 唯一的配置真源
# [IN] .ilang/site.ilang  [OUT] 一个 dict 给 scraper.py 和 build.py 读
# [RULE] 不许在别的文件里另写一份厂商清单 改了 site.ilang 站上必须变
# [BOUNDARY] never:把配置硬编码进代码|scope:permanent
# ::END

"""Minimal parser for the I-Lang config format used by this repo.

The format is line based:

    ILANG
    TYPE:config PROJECT:<name> LANG:<lang>

    ::STATE{@SITE, key:value, key:value}
    ::MODULE{NAME|title:...}
      row | row | row
      key: value
      ::RULE{...}
    ::END

This module is deliberately dependency free (stdlib only) so the whole
pipeline runs on a bare GitHub Actions runner with no pip install.
"""

from __future__ import annotations

import re
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / ".ilang" / "site.ilang"

_MODULE_RE = re.compile(r"^::MODULE\{([A-Za-z0-9_]+)\s*(?:\|(.*?))?\}\s*$")
_STATE_RE = re.compile(r"^::STATE\{(.*)\}\s*$")
_TAGGED_RE = re.compile(r"^::(RULE|BOUNDARY|FACT|LESSON|MUST|NEVER|CHECK)\{(.*)\}\s*$")


def _split_kv(blob: str) -> dict:
    """Parse `a:1, b:2` into {'a': '1', 'b': '2'} (commas inside values are kept)."""
    out: dict[str, str] = {}
    for chunk in blob.split(","):
        if ":" not in chunk:
            continue
        key, _, value = chunk.partition(":")
        out[key.strip()] = value.strip()
    return out


def _clean(line: str) -> str:
    return line.strip()


def parse_ilang(path: str | Path = DEFAULT_CONFIG_PATH) -> dict:
    """Read an I-Lang config file and return a plain dict."""
    text = Path(path).read_text(encoding="utf-8")
    lines = text.splitlines()

    cfg: dict = {
        "path": str(path),
        "head": {},
        "state": {},
        "modules": {},
        "rules": [],
        "boundaries": [],
    }

    current: str | None = None
    for raw in lines:
        line = _clean(raw)
        if not line or line == "ILANG" or line == "::END":
            continue

        # second line: TYPE:config PROJECT:x LANG:y
        if line.upper().startswith("TYPE:"):
            for token in line.split():
                if ":" in token:
                    k, _, v = token.partition(":")
                    cfg["head"][k.lower()] = v
            continue

        m = _MODULE_RE.match(line)
        if m:
            name = m.group(1).upper()
            title = (m.group(2) or "").strip()
            if title.lower().startswith("title:"):
                title = title[6:].strip()
            cfg["modules"][name] = {"title": title, "rows": [], "kv": {}, "rules": [], "boundaries": []}
            current = name
            continue

        m = _STATE_RE.match(line)
        if m:
            cfg["state"].update(_split_kv(m.group(1)))
            continue

        m = _TAGGED_RE.match(line)
        if m:
            tag, body = m.group(1).upper(), m.group(2).strip()
            if current and tag in ("RULE", "BOUNDARY"):
                bucket = "rules" if tag == "RULE" else "boundaries"
                cfg["modules"][current][bucket].append(body)
            elif tag == "RULE":
                cfg["rules"].append(body)
            elif tag == "BOUNDARY":
                cfg["boundaries"].append(body)
            continue

        # inside a module block
        if current:
            block = cfg["modules"][current]
            if "|" in line:
                block["rows"].append([c.strip() for c in line.split("|")])
            elif re.match(r"^[A-Za-z0-9_]+\s*:", line):
                k, _, v = line.partition(":")
                block["kv"][k.strip()] = v.strip()
            else:
                block["rows"].append([line])

    return cfg


# --------------------------------------------------------------------------
# typed accessors used by scraper.py / build.py
# --------------------------------------------------------------------------

def get_site(cfg: dict) -> dict:
    s = cfg.get("state", {})
    return {
        "brand": s.get("brand", "CouponScout"),
        "niche": s.get("niche", "consumer brand coupons"),
        "domain": s.get("domain", "couponscout.pages.dev"),
        "locale": s.get("locale", "en-US"),
        "currency": s.get("currency", "USD"),
    }


def get_providers(cfg: dict) -> list[dict]:
    """Return [{'name','homepage','promo','affiliate'}, ...] from MODULE{PROVIDERS}."""
    rows = cfg.get("modules", {}).get("PROVIDERS", {}).get("rows", [])
    out = []
    for row in rows:
        if not row or not row[0]:
            continue
        padded = row + [""] * (4 - len(row))
        out.append({
            "name": padded[0],
            "homepage": padded[1],
            "promo": padded[2],
            "affiliate": padded[3],
        })
    return out


def get_categories(cfg: dict) -> dict:
    rows = cfg.get("modules", {}).get("CATEGORIES", {}).get("rows", [])
    out = {}
    for row in rows:
        if len(row) >= 2 and row[0]:
            out[row[0]] = row[1]
    return out


def get_fields(cfg: dict) -> list[str]:
    rows = cfg.get("modules", {}).get("FIELDS", {}).get("rows", [])
    if not rows:
        return ["title", "price", "currency", "offer_url", "valid_until", "source_url", "fetched_at"]
    return rows[0][0].split()


def _typed(value: str):
    v = value.strip()
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def get_settings(cfg: dict) -> dict:
    kv = cfg.get("modules", {}).get("SETTINGS", {}).get("kv", {})
    defaults = {
        "user_agent": "PromoRadarBot/1.0",
        "request_timeout": 25,
        "request_delay": 1.2,
        "max_pages_per_provider": 14,
        "max_offers_per_provider": 40,
        "min_discount_percent": 5,
        "discount_only": True,
        "obey_robots": True,
        "shopify_feed_limit": 250,
    }
    out = dict(defaults)
    for k, v in kv.items():
        out[k] = _typed(v)
    return out


def get_render(cfg: dict) -> dict:
    kv = cfg.get("modules", {}).get("RENDER", {}).get("kv", {})
    defaults = {
        "site_title": get_site(cfg)["brand"],
        "site_tagline": "Verified brand promo pages, refreshed automatically.",
        "items_per_page": 60,
        "show_source_url": True,
        "show_fetched_at": True,
        "nav_order": "",
    }
    out = dict(defaults)
    for k, v in kv.items():
        out[k] = _typed(v)
    return out


if __name__ == "__main__":  # tiny self-check
    import json
    c = parse_ilang()
    print(json.dumps({
        "head": c["head"],
        "site": get_site(c),
        "providers": len(get_providers(c)),
        "categories": get_categories(c),
        "fields": get_fields(c),
        "settings": get_settings(c),
        "render": get_render(c),
    }, indent=2, ensure_ascii=False))
