# CouponScout Promo Radar

An independent tracker of official brand promo pages and live discounts, in four categories:
apparel, beauty, tech accessories and home appliances.

**Live site:** https://couponscout.pages.dev
**Data refreshed:** every 6 hours by a scheduled job, no server and no paid service involved.

## What this actually is

A brand's real prices live on the brand's own site. Most deal sites restate them from memory or
from an affiliate network feed. This one reads them directly:

| Tier | Source | What it gives us |
| --- | --- | --- |
| A | `<origin>/products.json` | Price and `compare_at_price` per variant |
| A2 | `<origin>/collections/<sale>/products.json` | The brand's own sale collection, verbatim |
| B | Sitemap, then schema.org JSON-LD on each product page | `Offer.price`, `priceCurrency`, strikethrough price |
| C | Nothing machine-readable | We link the official promo page and list no offers |

Two classes of listing, and the site never blurs them:

- **Verified discount** - the page carried a reference price, so the percentage shown is
  arithmetic on two published numbers.
- **On brand sale page** - the item sits on a page the brand itself designates as a sale or
  promotion page. We report the real price and claim no percentage, because there was no
  reference price to compute one from.

Nothing on the site is estimated, and nothing is written by hand. If a brand publishes no
machine-readable offer data, its page says so and links its promo page instead. A thinner page
beats a page with numbers we made up.

## How it runs

```
scraper.py                    reads each brand's public pages -> data/offers.json
build.py                      reads that dataset + templates/ -> site/
templates/                    the HTML and CSS, no framework
.ilang/site.ilang             the config: which brands, which fields, which rules
.github/workflows/update.yml  cron every 6 hours: scrape, build, commit
site/_manifest.json           what the last build produced, so rebuilds stay incremental
```

The whole pipeline is pure Python standard library. No API keys, no LLM calls at runtime, no
dependencies to install, nothing to pay for. A public repository gets unlimited GitHub Actions
minutes, so the scheduled rebuild costs nothing.

`build.py` does not wipe and regenerate `site/`. It keeps a manifest and retires only the pages
that dropped out of the current build, so a rebuild touches a handful of files instead of
re-writing the whole tree, and nothing unexpected in that directory gets destroyed.

Product images are the brands' own, hotlinked from their CDN and never copied or re-hosted.
When a brand publishes no image, the card renders a plain placeholder tile rather than a broken
image.

## Using it

Locally:

```bash
python scraper.py     # refresh data/offers.json from the brands' public pages
python build.py       # regenerate site/
python -m http.server 8080 --directory site    # look at it
```

To track a different brand, edit the `PROVIDERS` block in `.ilang/site.ilang` and re-run. Both
scripts read that file; there is no second copy of the brand list anywhere in the code.

To deploy: connect this repository to Cloudflare Pages with build command `python build.py` and
output directory `site/`.

## What this site does not do

- It does not scrape anything behind a login, and it obeys `robots.txt`.
- It does not invent prices, discounts, expiry dates or commission rates.
- It does not do brand-name bidding, cookie stuffing, or self-referral.
- It is not affiliated with, endorsed by or sponsored by any brand listed.

Prices change without warning. Always confirm on the brand's page before buying.

---

Site rules are described in the I-Lang protocol - see `.ilang/site.ilang`. Protocol reference: ilang.ai
