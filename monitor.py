#!/usr/bin/env python3
"""
Palworld OCG restock/new-product monitor.

Checks a list of sites (configured in sites_config.json) for Palworld OCG
products, compares against last-known state (state.json), and notifies you
(SMS via Twilio, and/or email) when something new appears or a sold-out
item comes back in stock. The two channels are independent - a failure in
one (e.g. a Twilio account issue) doesn't block the other, and the run only
fails (which keeps state.json from advancing, so you don't lose the alert)
if BOTH channels fail.

Run this once per invocation (e.g. via cron at :00 and :30, or via a
scheduled GitHub Actions workflow). It is NOT a long-running daemon.
"""

import base64
import json
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "sites_config.json"
STATE_PATH = BASE_DIR / "state.json"

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


# ---------------------------------------------------------------------------
# Site parsers — each returns a list of dicts: {id, name, price, in_stock, url}
# ---------------------------------------------------------------------------

def variant_is_available(variant):
    """
    Unlike the /products.json collection endpoint, the per-product
    /products/{handle}.json endpoint doesn't include an `available`
    field on variants - so availability has to be derived from
    inventory_management/policy/quantity the same way Shopify itself
    computes it.
    """
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
        pr = requests.get(f"{base}/products/{handle}.json", headers=HEADERS, timeout=15)
        if pr.status_code != 200:
            continue
        p = pr.json().get("product")
        if not p:
            continue
        for variant in p.get("variants", []):
            products.append({
                "id": f"{p['id']}-{variant['id']}",
                "name": f"{p['title']} ({variant.get('title','Default')})".replace(" (Default Title)", ""),
                "price": variant.get("price"),
                "in_stock": variant_is_available(variant),
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

def send_sms(message):
    from twilio.rest import Client

    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    from_number = os.environ["TWILIO_FROM_NUMBER"]
    to_number = os.environ["TWILIO_TO_NUMBER"]

    client = Client(sid, token)
    # SMS is capped at 1600 chars per message (Twilio auto-splits), keep it short anyway
    client.messages.create(body=message[:1500], from_=from_number, to=to_number)


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
# Main
# ---------------------------------------------------------------------------

def main():
    config = load_json(CONFIG_PATH, {"keyword": "palworld", "sites": []})
    keyword = config.get("keyword", "palworld")
    is_first_run = not STATE_PATH.exists()
    state = load_json(STATE_PATH, {})

    new_state = {}
    alerts = []

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

        for p in products:
            key = f"{site['name']}::{p['id']}"
            prev = state.get(key)
            new_state[key] = {
                "name": p["name"],
                "url": p["url"],
                "in_stock": p["in_stock"],
                "price": p["price"],
            }

            is_new_listing = prev is None
            restocked = prev is not None and (not prev["in_stock"]) and p["in_stock"]

            if p["in_stock"] and (is_new_listing or restocked):
                tag = "NEW" if is_new_listing else "RESTOCK"
                price_str = f"${p['price']}" if p["price"] else "price unknown"
                alerts.append(
                    f"[{tag}] {site['name']}: {p['name']} - {price_str}\n{p['url']}"
                )

    # Carry forward anything we didn't see this run (e.g. temporary fetch
    # failure) so we don't lose track of it / re-alert incorrectly.
    for key, val in state.items():
        new_state.setdefault(key, val)

    save_json(STATE_PATH, new_state)

    if is_first_run:
        # Nothing to compare against yet, so everything looks "new" purely
        # because we've never tracked it before - not because it actually
        # just restocked. Establish the baseline silently; only alert on
        # genuine changes from here on.
        print(f"First run: recorded {len(new_state)} product(s) as baseline. No texts sent.")
    elif alerts:
        message = "Palworld OCG alert:\n\n" + "\n\n".join(alerts)
        print(message)

        notified = False
        try:
            send_sms(message)
            notified = True
        except Exception as e:
            print(f"[ERROR] SMS send failed: {e}", file=sys.stderr)

        try:
            send_email("Palworld OCG Alert", message)
            notified = True
        except Exception as e:
            print(f"[ERROR] Email send failed: {e}", file=sys.stderr)

        if not notified:
            # Both channels failed - raise so this run exits non-zero and
            # state.json doesn't get committed. Next run will see the same
            # unchanged state and retry the alert instead of losing it.
            raise RuntimeError("Both SMS and email notifications failed - see errors above")
    else:
        print("No changes this run.")


if __name__ == "__main__":
    main()
