#!/usr/bin/env python3
"""
Palworld OCG restock/new-product monitor.

Checks a list of sites (configured in sites_config.json) for Palworld OCG
products, compares against last-known state (state.json), and sends an SMS
via Twilio when something new appears or a sold-out item comes back in stock.

Run this once per invocation (e.g. via cron at :00 and :30, or via a
scheduled GitHub Actions workflow). It is NOT a long-running daemon.
"""

import json
import os
import re
import sys
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

def parse_shopify(site, keyword):
    """
    Works for ANY Shopify-based storefront (very common for TCG shops).
    Shopify publicly exposes a JSON product feed at /products.json — no
    scraping/selectors needed, just filter by keyword.
    """
    products = []
    page = 1
    base = site["base_url"].rstrip("/")
    while True:
        url = f"{base}/products.json?limit=250&page={page}"
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            break
        data = r.json().get("products", [])
        if not data:
            break
        for p in data:
            haystack = f"{p.get('title','')} {p.get('product_type','')} {' '.join(p.get('tags',[]))}".lower()
            if keyword.lower() not in haystack:
                continue
            for variant in p.get("variants", []):
                products.append({
                    "id": f"{p['id']}-{variant['id']}",
                    "name": f"{p['title']} ({variant.get('title','Default')})".replace(" (Default Title)", ""),
                    "price": variant.get("price"),
                    "in_stock": bool(variant.get("available")),
                    "url": f"{base}/products/{p.get('handle')}",
                })
        page += 1
        if page > 20:  # safety cap
            break
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


PARSERS = {
    "shopify": parse_shopify,
    "html": parse_html,
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config = load_json(CONFIG_PATH, {"keyword": "palworld", "sites": []})
    keyword = config.get("keyword", "palworld")
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
            new_state[key] = {"in_stock": p["in_stock"], "price": p["price"]}

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

    if alerts:
        message = "Palworld OCG alert:\n\n" + "\n\n".join(alerts)
        print(message)
        send_sms(message)
    else:
        print("No changes this run.")


if __name__ == "__main__":
    main()
