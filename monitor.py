#!/usr/bin/env python3
"""
Palworld OCG restock/new-product monitor.

Checks a list of sites (configured in sites_config.json) for Palworld OCG
products, compares against last-known state (state.json), and notifies you
(ntfy push notification and email) when something new appears or a
sold-out item comes back in stock. The two channels are independent - a
failure in one doesn't block the other, and the run only fails (which
keeps state.json from advancing, so you don't lose the alert) if BOTH
channels fail.

Run this once per invocation (e.g. via cron at :00 and :30, or via a
scheduled GitHub Actions workflow). It is NOT a long-running daemon.
"""

import base64
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "sites_config.json"
STATE_PATH = BASE_DIR / "state.json"
DASHBOARD_PATH = BASE_DIR / "docs" / "index.html"

# Loads BASE_DIR/.env into the environment if it exists (local/PC/VPS use).
# On GitHub Actions there's no .env file - the workflow injects the same
# variable names directly, so this line is a harmless no-op there.
load_dotenv(BASE_DIR / ".env")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def load_json(path, default):
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def parse_price(value):
    """Best-effort numeric parse for price comparison - prices come from
    different parsers as strings ("164.99") or numbers (19.97), and are
    sometimes None (price unknown)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_price(usd_price):
    """
    Formats a USD amount for display. Every parser provides `usd_price`
    already converted (Shopify's own `?currency=USD` param does real
    merchant-configured conversion for non-USD stores - see parse_shopify);
    this function is purely presentational, not a conversion point.
    """
    amount = parse_price(usd_price)
    if amount is None:
        return "price unknown"
    return f"${amount:.2f}"


# ---------------------------------------------------------------------------
# Site parsers — each returns a list of dicts: {id, name, price, in_stock, url}
# ---------------------------------------------------------------------------

def variant_is_available(variant, product_available):
    """
    Unlike the /products.json collection endpoint, the per-product
    /products/{handle}.json endpoint doesn't include an `available` field
    on variants. Some stores still expose real inventory_management/
    policy/quantity there, which lets us compute availability precisely
    the way Shopify itself does. Many stores hide those fields entirely
    (a common merchant privacy setting) - in that case there's no signal
    at all in this response, so fall back to the product-level `available`
    flag already returned by the search endpoint (accurate, just not
    variant-granular - fine here since these products are almost always
    single-variant).
    """
    has_inventory_data = "inventory_quantity" in variant or "inventory_policy" in variant
    if not has_inventory_data:
        return bool(product_available)
    if variant.get("inventory_management") != "shopify":
        return True  # inventory not tracked -> always purchasable
    if variant.get("inventory_policy") == "continue":
        return True  # overselling allowed -> purchasable even at 0
    qty = variant.get("inventory_quantity")
    return bool(qty and qty > 0)


def parse_shopify(site, keyword):
    """
    Works for ANY Shopify-based storefront (very common for TCG shops).
    Uses Shopify's public predictive-search endpoint to find products
    matching `keyword` server-side (no need to page through the entire
    catalog), then fetches each match's full product JSON to get
    per-variant price/stock detail (the search endpoint itself doesn't
    include variants).
    Prices are fetched in the store's native currency for `price`/`currency`
    - used for price-drop comparisons - rather than Shopify's converted USD
    figure, which drifts by a dollar or two over the course of a day purely
    from the live exchange rate updating (seen in practice on Kongs Cards:
    repeated false "price drop" alerts between $168/$169 while the actual
    GBP price never changed). `usd_price` is a second, separate value used
    only for display, fetched via Shopify's `currency=USD` param - the
    merchant's own real converted price (actual configured rate/markup),
    not an estimate.
    """
    products = []
    base = site["base_url"].rstrip("/")

    search_url = f"{base}/search/suggest.json"
    params = {
        "q": keyword,
        "resources[type]": "product",
        "resources[limit]": 50,
        "resources[options][unavailable_products]": "show",
    }
    r = requests.get(search_url, headers=HEADERS, params=params, timeout=15)
    r.raise_for_status()
    matches = r.json().get("resources", {}).get("results", {}).get("products", [])

    for m in matches:
        handle = m.get("handle")
        if not handle:
            continue

        # Shopify's predictive search does fuzzy/relevance matching and can
        # return results with no real textual connection to the query (seen
        # in practice: an RPG game-master kit "matching" a "palworld"
        # search). Re-check the keyword actually appears somewhere on the
        # product before trusting the match. Some stores (Topspot) put the
        # keyword only in `vendor` - e.g. a product titled just by its SKU
        # code with vendor "Palworld" - so title/type/tags alone isn't
        # enough; missing vendor here previously caused every Topspot
        # product to look unmatched and get marked delisted.
        haystack = (
            f"{m.get('title','')} {m.get('type','')} {m.get('vendor','')} "
            f"{' '.join(m.get('tags',[]))}"
        ).lower()
        if keyword.lower() not in haystack:
            continue

        pr = requests.get(f"{base}/products/{handle}.json", headers=HEADERS, timeout=15)
        if pr.status_code != 200:
            continue
        p = pr.json().get("product")
        if not p:
            continue

        variants = p.get("variants", [])
        native_currency = (variants[0].get("price_currency") if variants else None) or "USD"

        usd_prices_by_variant = {}
        if native_currency != "USD":
            pr_usd = requests.get(
                f"{base}/products/{handle}.json",
                headers=HEADERS,
                params={"currency": "USD"},
                timeout=15,
            )
            if pr_usd.status_code == 200:
                p_usd = pr_usd.json().get("product") or {}
                usd_prices_by_variant = {v["id"]: v.get("price") for v in p_usd.get("variants", [])}

        for variant in variants:
            native_price = variant.get("price")
            if native_currency == "USD":
                usd_price = native_price
            else:
                usd_price = usd_prices_by_variant.get(variant["id"], native_price)
            products.append({
                "id": f"{p['id']}-{variant['id']}",
                "name": f"{p['title']} ({variant.get('title','Default')})".replace(" (Default Title)", ""),
                "price": native_price,
                "currency": native_currency,
                "usd_price": usd_price,
                "in_stock": variant_is_available(variant, m.get("available")),
                "url": f"{base}/products/{p.get('handle')}",
            })
    return products


def parse_html(site, keyword):
    """
    Generic scraper for a normal (non-Shopify) storefront using CSS
    selectors you define per-site in sites_config.json. Only works for
    server-rendered HTML — if the site loads products via JavaScript,
    this will come back empty and you'll need a headless-browser
    approach (Playwright) instead.
    """
    products = []
    sel = site["selectors"]
    r = requests.get(site["url"], headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    for card in soup.select(sel["product_container"]):
        name_el = card.select_one(sel["name"])
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        if keyword.lower() not in name.lower():
            continue

        price_el = card.select_one(sel["price"])
        price = None
        if price_el:
            match = re.search(r"[\d,]+\.?\d*", price_el.get_text(strip=True))
            if match:
                price = match.group().replace(",", "")

        stock_el = card.select_one(sel.get("stock_indicator", ""))
        in_stock = True
        if stock_el:
            in_stock = sel.get("in_stock_text", "in stock").lower() in stock_el.get_text(strip=True).lower()

        link_el = card.select_one("a")
        url = link_el["href"] if link_el and link_el.has_attr("href") else site["url"]
        if url.startswith("/"):
            from urllib.parse import urljoin
            url = urljoin(site["url"], url)

        products.append({
            "id": re.sub(r"\s+", "-", name.lower()),
            "name": name,
            "price": price,
            "currency": site.get("currency", "USD"),
            "usd_price": price,
            "in_stock": in_stock,
            "url": url,
        })
    return products


def parse_bigcommerce_bodl(site, keyword):
    """
    For BigCommerce (Stencil) storefronts, like Game Nerdz, whose product
    grid is rendered client-side by a third-party search widget - a plain
    GET of the category page returns an "empty" grid, nothing to select.

    However, BigCommerce's own analytics data layer ("BODL") embeds a
    base64-encoded JSON blob directly in the server-rendered page (a
    `bodl_v1_product_category_viewed` tracking event), which lists every
    product on the page with accurate name/price/stock - so we read that
    instead of the (JS-only) visible grid.

    `quantity` in that blob is the real inventory count: 0 means sold out,
    None means untracked/unlimited inventory (e.g. an open preorder), and
    a positive number means that many are in stock. There's no per-product
    URL in this data (real slugs are only generated client-side), so every
    alert links back to the category page itself.
    """
    r = requests.get(site["url"], headers=HEADERS, timeout=15)
    r.raise_for_status()

    match = re.search(r'decodeBase64\("([^"]+)"\)', r.text)
    if not match:
        raise ValueError("BODL analytics blob not found in page - site markup may have changed")
    data = json.loads(base64.b64decode(match.group(1)))

    line_items = []
    for event in data.get("events", []):
        for payload in event.values():
            if "line_items" in payload:
                line_items.extend(payload["line_items"])

    products = []
    for item in line_items:
        name = item.get("product_name", "")
        if keyword.lower() not in name.lower():
            continue
        quantity = item.get("quantity")
        products.append({
            "id": item.get("sku") or item.get("product_id"),
            "name": name,
            "price": item.get("purchase_price"),
            "currency": item.get("currency") or site.get("currency", "USD"),
            "usd_price": item.get("purchase_price"),
            "in_stock": quantity is None or quantity > 0,
            "url": site["url"],
        })
    return products


PARSERS = {
    "shopify": parse_shopify,
    "html": parse_html,
    "bigcommerce_bodl": parse_bigcommerce_bodl,
}


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def send_ntfy(subject, body):
    topic = os.environ["NTFY_TOPIC"]
    r = requests.post(
        f"https://ntfy.sh/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": subject},
        timeout=15,
    )
    r.raise_for_status()


def send_email(subject, body):
    from_addr = os.environ["EMAIL_FROM"]
    app_password = os.environ["EMAIL_APP_PASSWORD"]
    to_addr = os.environ["EMAIL_TO"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as smtp:
        smtp.starttls()
        smtp.login(from_addr, app_password)
        smtp.send_message(msg)


# ---------------------------------------------------------------------------
# Dashboard - a per-store/per-product stock grid for the core Palworld TCG
# items, written to docs/index.html for GitHub Pages. Product names are
# free text and vary a lot between stores, so classify_product() matches
# on the same core phrases each store actually uses rather than relying
# on any single store's naming convention.
# ---------------------------------------------------------------------------

# Column order follows release date: Dawn of Palpagos (BP01/TD01/TD02), the
# Sleeve & Card Set, Legends Awaken (BP02), then the 3rd set, "Eternal
# Ascent" (TD03/TD04/BP03).
DASHBOARD_CATEGORIES = ["BP01", "TD01", "TD02", "SLEEVE", "BP02", "TD03", "TD04", "BP03"]
DASHBOARD_LABELS = {
    "BP01": "BP01", "TD01": "TD01", "TD02": "TD02", "SLEEVE": "Sleeve Set",
    "BP02": "BP02", "TD03": "TD03", "TD04": "TD04", "BP03": "BP03",
}


def classify_product(name):
    """
    Maps a free-text product name to one of the core single-unit items
    tracked on the dashboard, or None if it's a bulk/variant product
    (booster case, trial deck display, single booster pack, individual
    character sleeves, playmats, etc.) or something else entirely.
    """
    n = name.lower()
    # "Booster Display" (with or without a trailing "Box") is some stores'
    # own name for the ordinary single retail box, not a multi-box bundle -
    # confirmed via product descriptions: Gamers Guild AZ's "Booster Display
    # Box" ("12 packs per box, 12 boxes per case") and Lazarus Games' bare
    # "Booster Display" ("12 Packs per Display") are both the standard
    # single box. Only exclude "display" when it's NOT immediately preceded
    # by "booster" - that still catches genuine bulk multiples ("[x6] Trial
    # Deck Display", "... Box Display" (reversed order), "Display (6
    # Sets)"), and "Booster Display Case" still correctly excludes via case.
    is_bulk_case = "case" in n
    is_bulk_display = "display" in n and "booster display" not in n
    if is_bulk_case or is_bulk_display or re.search(r"\bbooster pack\b(?!s)", n):
        return None

    has_dop = "dawn of palpagos" in n
    has_la = "legends awaken" in n
    # "set 3" was Topspot's placeholder before the set had a name; it's now
    # officially "Eternal Ascent" - keep matching both in case any store
    # still shows the old placeholder text.
    has_set3 = "set 3" in n or "eternal ascent" in n
    # "booster display" (see note above) doesn't contain the literal
    # substring "booster box" - needs its own check even though it's the
    # same single-unit product.
    has_box = "booster box" in n or "booster display" in n
    has_td = "trial deck" in n
    has_red = "red" in n
    has_blue = "blue" in n
    has_green = "green" in n
    has_purple = "purple" in n
    has_sleeve = "sleeve" in n
    has_card_set = "card set" in n

    if has_dop and has_box:
        return "BP01"
    if has_la and has_box:
        return "BP02"
    if has_set3 and has_box:
        return "BP03"
    if has_td and has_red and has_blue:
        return "TD01"
    if has_td and has_green and has_purple:
        return "TD02"
    # Eternal Ascent's trial decks are Red/Green (TD03) and Blue/Purple
    # (TD04) - some stores (Game Nerdz) name them by color pair only, with
    # no "TD03"/"TD04"/"Trial Deck 3" text anywhere in the product name
    # (only in their internal SKU, which classify_product never sees), so
    # the explicit marker check alone missed them.
    if has_set3 and has_td and (("trial deck 3" in n or "td03" in n) or (has_red and has_green)):
        return "TD03"
    if has_set3 and has_td and (("trial deck 4" in n or "td04" in n) or (has_blue and has_purple)):
        return "TD04"
    if has_sleeve and has_card_set:
        return "SLEEVE"
    return None


def generate_dashboard(state, sites):
    site_names = [s["name"] for s in sites if s.get("enabled", True)]
    grid = {name: {cat: None for cat in DASHBOARD_CATEGORIES} for name in site_names}

    for key, entry in state.items():
        site_name = key.split("::", 1)[0]
        if site_name not in grid:
            continue
        category = classify_product(entry["name"])
        if not category:
            continue
        cell = grid[site_name][category]
        # A store can carry multiple variants of the same item (e.g. CCGPrime's
        # per-location listings) - if any variant is in stock, show it as in
        # stock rather than whichever variant happened to be seen last.
        if cell is None or (entry["in_stock"] and not cell["in_stock"]):
            grid[site_name][category] = {
                "in_stock": entry["in_stock"],
                "url": entry["url"],
                "usd_price": entry.get("usd_price", entry["price"]),
            }

    updated = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M %p %Z")

    def escape(text):
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))

    rows_html = []
    for site_name in site_names:
        cells = []
        for cat in DASHBOARD_CATEGORIES:
            cell = grid[site_name][cat]
            if cell is None:
                cells.append('<td class="not-carried">&mdash;</td>')
            elif cell["in_stock"]:
                price_str = f' {escape(format_price(cell["usd_price"]))}' if cell["usd_price"] else ""
                cells.append(
                    f'<td class="in-stock"><a href="{escape(cell["url"])}" '
                    f'target="_blank" rel="noopener">&check;{price_str}</a></td>'
                )
            else:
                cells.append(
                    f'<td class="sold-out"><a href="{escape(cell["url"])}" '
                    f'target="_blank" rel="noopener">&cross;</a></td>'
                )
        rows_html.append(f"<tr><td class=\"site-name\">{escape(site_name)}</td>{''.join(cells)}</tr>")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Palworld OCG Stock Tracker</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          margin: 2rem; background: #111318; color: #e8e8ea; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
  .updated {{ color: #8a8f98; font-size: 0.9rem; margin-bottom: 1.5rem; }}
  .legend {{ color: #8a8f98; font-size: 0.85rem; margin-bottom: 1rem; }}
  table {{ border-collapse: collapse; width: 100%; max-width: 920px; }}
  th, td {{ border: 1px solid #2a2d35; padding: 0.5rem 0.6rem; text-align: center; }}
  th {{ background: #1b1e25; font-weight: 600; }}
  td.site-name {{ text-align: left; font-weight: 600; }}
  td.in-stock {{ background: #123120; }}
  td.in-stock a {{ color: #4ade80; text-decoration: none; font-weight: 700; font-size: 1.05rem; }}
  td.in-stock a:hover {{ text-decoration: underline; }}
  td.sold-out {{ background: #201415; color: #ef6a6a; }}
  td.sold-out a {{ color: #ef6a6a; text-decoration: none; }}
  td.not-carried {{ color: #4a4d55; }}
</style>
</head>
<body>
<h1>Palworld OCG Stock Tracker</h1>
<div class="updated">Last updated: {updated}</div>
<div class="legend">BP01/TD01/TD02 = Dawn of Palpagos (Booster Box, Red/Blue Trial Deck, Green/Purple Trial Deck) &middot;
Sleeve Set = Sleeve &amp; Card Set Vol. 1 &middot; BP02 = Legends Awaken Booster Box &middot;
TD03/TD04/BP03 = Eternal Ascent (Red/Green Trial Deck, Blue/Purple Trial Deck, Booster Box)</div>
<table>
<tr><th>Store</th>{''.join(f'<th>{DASHBOARD_LABELS[cat]}</th>' for cat in DASHBOARD_CATEGORIES)}</tr>
{''.join(rows_html)}
</table>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config = load_json(CONFIG_PATH, {"keyword": "palworld", "sites": []})
    keyword = config.get("keyword", "palworld")
    is_first_run = not STATE_PATH.exists()
    state = load_json(STATE_PATH, {})

    new_state = {}
    alerts = []
    succeeded_sites = set()

    for site in config["sites"]:
        if not site.get("enabled", True):
            continue

        parser = PARSERS.get(site["type"])
        if not parser:
            print(f"[WARN] Unknown site type '{site['type']}' for {site['name']}", file=sys.stderr)
            continue

        try:
            products = parser(site, keyword)
        except Exception as e:
            print(f"[ERROR] {site['name']}: {e}", file=sys.stderr)
            continue

        # A site that previously had tracked products but suddenly returns
        # none is more likely a scrape bug (seen in practice: a keyword
        # safety-check too strict for one store's naming, silently zeroing
        # out real results) than every single item actually being delisted
        # at once. Treat that as a failure - preserve existing state rather
        # than mass-marking everything sold out - while still trusting a
        # genuine 0 for a site with no prior history (e.g. right after
        # being added).
        had_prior_entries = any(k.startswith(f"{site['name']}::") for k in state)
        if not products and had_prior_entries:
            print(
                f"[WARN] {site['name']}: returned 0 products but previously had "
                f"tracked entries - treating as a likely scrape issue, not a real "
                f"mass delisting. Existing state preserved untouched.",
                file=sys.stderr,
            )
            continue

        succeeded_sites.add(site["name"])
        for p in products:
            key = f"{site['name']}::{p['id']}"
            prev = state.get(key)
            new_state[key] = {
                "name": p["name"],
                "url": p["url"],
                "in_stock": p["in_stock"],
                "price": p["price"],
                "currency": p.get("currency", "USD"),
                "usd_price": p.get("usd_price", p["price"]),
            }

            is_new_listing = prev is None
            restocked = prev is not None and (not prev["in_stock"]) and p["in_stock"]

            # Only counts as a "price drop" when it was already in stock and
            # still is - an item coming back in stock at a lower price than
            # it had while sold out is just a restock, not a separate event.
            prev_price = parse_price(prev["price"]) if prev else None
            curr_price = parse_price(p["price"])
            price_dropped = (
                prev is not None
                and prev["in_stock"]
                and p["in_stock"]
                and prev_price is not None
                and curr_price is not None
                and curr_price < prev_price
            )

            if p["in_stock"] and (is_new_listing or restocked or price_dropped):
                if is_new_listing:
                    tag = "NEW"
                elif restocked:
                    tag = "RESTOCK"
                else:
                    tag = "PRICE DROP"

                if tag == "PRICE DROP":
                    prev_usd = prev.get("usd_price", prev["price"])
                    price_str = f"{format_price(prev_usd)} -> {format_price(p.get('usd_price', p['price']))}"
                else:
                    price_str = format_price(p.get("usd_price", p["price"]))

                alerts.append(
                    f"[{tag}] {site['name']}: {p['name']} - {price_str}\n{p['url']}"
                )

    # Carry forward anything we didn't see this run. If its site failed
    # entirely this run (transient error), keep the entry exactly as-is so
    # a temporary blip doesn't lose track of it or misfire an alert. If the
    # site succeeded but this specific product just wasn't in the results,
    # it's been delisted - mark it out of stock rather than leaving a
    # possibly-stale "in stock" forever (this also keeps the dashboard
    # honest about what's actually still being sold).
    for key, val in state.items():
        if key in new_state:
            continue
        site_name = key.split("::", 1)[0]
        if site_name in succeeded_sites:
            new_state[key] = {**val, "in_stock": False}
        else:
            new_state[key] = val

    save_json(STATE_PATH, new_state)

    DASHBOARD_PATH.parent.mkdir(exist_ok=True)
    DASHBOARD_PATH.write_text(generate_dashboard(new_state, config["sites"]), encoding="utf-8")

    if is_first_run:
        # Nothing to compare against yet, so everything looks "new" purely
        # because we've never tracked it before - not because it actually
        # just restocked. Establish the baseline silently; only alert on
        # genuine changes from here on.
        print(f"First run: recorded {len(new_state)} product(s) as baseline. No notification sent.")
    elif alerts:
        message = "Palworld OCG alert:\n\n" + "\n\n".join(alerts)
        print(message)

        notified = False
        try:
            send_ntfy("Palworld OCG Alert", message)
            notified = True
        except Exception as e:
            print(f"[ERROR] ntfy send failed: {e}", file=sys.stderr)

        try:
            send_email("Palworld OCG Alert", message)
            notified = True
        except Exception as e:
            print(f"[ERROR] Email send failed: {e}", file=sys.stderr)

        if not notified:
            # Both channels failed - raise so this run exits non-zero and
            # state.json doesn't get committed. Next run will see the same
            # unchanged state and retry the alert instead of losing it.
            raise RuntimeError("Both ntfy and email notifications failed - see errors above")
    else:
        print("No changes this run.")


if __name__ == "__main__":
    main()
