# Palworld OCG Monitor

Checks your listed sites every 30 min for new/restocked Palworld OCG
products and sends you a push notification (ntfy) and an email when
something shows up.

## 1. Your sites (already filled in)

`sites_config.json` is configured for 15 sites:

| Site | Type | Status |
|---|---|---|
| Flipside Gaming | Shopify | Ready |
| Topspot Cards | Shopify | Ready |
| VGMX | Shopify | Ready |
| Lumius Inc | Shopify | Ready |
| Gamers Getaway KC | Shopify | Ready |
| Moonlight Collectibles | Shopify | Ready |
| Game Nerdz | BigCommerce (`bigcommerce_bodl`) | Ready |
| Gamers Guild AZ | Shopify | Ready |
| Paladin Cards | Shopify | Ready |
| Lazarus Games | Shopify | Ready |
| The Card Vault | Shopify | Ready |
| Kongs Cards | Shopify | Ready |
| TCG Guy Hub | Wix (`html`) | Ready |
| Miniature Market | Shopware (`html`) | Ready |
| CCGPrime | Shopify | Ready |

12 of the 15 run on Shopify, so they use the search-based `products.json`
approach with no scraping guesswork. Game Nerdz runs on BigCommerce with
a JS-rendered product grid (nothing usable in the plain HTML), so it
instead reads the analytics data blob BigCommerce embeds in the
server-rendered page - see `parse_bigcommerce_bodl()` in `monitor.py`.
One tradeoff: there's no per-product URL in that data (real product
slugs are only generated client-side), so Game Nerdz alerts link back to
the category page rather than the exact product.

TCG Guy Hub (Wix) and Miniature Market (Shopware) both server-render
their category pages, so the generic `html` scraper works directly
against each platform's own markup. Miniature Market's URL is
pre-filtered to their "Palworld" brand property so it doesn't have to
scan their whole "Other CCGs" catalog.

CCGPrime was requested as a `shop.app/m/...` link (Shopify's own "Shop
app" storefront listing), not the merchant's real domain. Resolved that
to their actual Shopify store at `ccgprime.com` and used the standard
approach against that instead - shop.app's own availability display was
out of sync with the real storefront during testing, so querying the
merchant's own store directly is both simpler and more trustworthy.

**Not added:** `magicmadhouse.co.uk` (BigCommerce). Unlike Game Nerdz,
its product grid is rendered entirely by a third-party search widget
(Klevu) with no accessible plain-HTTP data source - no BODL blob, no
server-rendered product markup. Adding it would require either a
headless browser or reverse-engineering Klevu's private search API.

I didn't add TCGplayer, Amazon, or the official Palworld TCG site —
TCGplayer requires a paid API/approval process, Amazon actively blocks
scraping, and the official site is mostly a "find a retailer" hub
rather than a direct storefront. If you want any of those added,
say the word and I'll figure out the right approach for each
(TCGplayer in particular has a real API worth using instead of scraping).

To add more sites later, just add another entry to `sites_config.json`
following the same pattern — Shopify ones need only the base URL.

Two site "types" are supported:

- **`shopify`** — use this if the store runs on Shopify (most do — check if
  `yourstore.com/products.json` returns JSON in your browser). You only
  need the base URL, nothing else. This is the most reliable option since
  it reads the store's own product API instead of scraping HTML.
- **`html`** — for everything else. You'll need to give it CSS selectors
  for the product card, name, price, and stock status on that page. This
  only works if the site renders products server-side (view page source —
  if you see the product names in the raw HTML, you're fine; if the page
  is nearly empty until JS runs, this approach won't work and the site
  needs a different tool — Playwright — let me know if you hit this).

Set `"enabled": true` on each site once configured. Send me a site's URL
and I'll figure out the right approach and write the config for you.

## 2. Notifications (ntfy push + email)

Both channels are independent - if one fails, the other still goes out
and the run still succeeds. It only fails (which is what keeps
`state.json` from advancing, so you don't lose an alert) if **both**
fail.

### Push notifications via ntfy

1. Install the [ntfy app](https://ntfy.sh/) (iOS/Android) or use the web app.
2. Subscribe to your topic (ask me for it, or generate your own — see
   below). ntfy topics are public and unauthenticated: anyone who knows
   the exact topic name can read or post to it, so it needs to be an
   unguessable random string, not something like `palworld-stock`.
3. `NTFY_TOPIC` is just that topic name — no account or API key needed.

### Email via Gmail

1. Use any Gmail account (a throwaway one is fine) as the sender.
2. Generate a Gmail **App Password** for it: Google Account → Security →
   2-Step Verification (must be enabled) → App passwords. Takes a couple
   minutes, no review/approval wait.
3. `EMAIL_TO` is just the destination address you want alerts sent to.

### Setting the values

Add these as GitHub Actions **repo secrets** (Settings → Secrets and
variables → Actions) if using the GitHub Actions route, or into a local
`.env` file for the PC/VM route:

```
NTFY_TOPIC=
EMAIL_FROM=
EMAIL_APP_PASSWORD=
EMAIL_TO=
```

## 3. Choose where it runs

### Option A — Your own PC or a free-tier cloud VM (e.g. Oracle Cloud Free Tier)

```bash
pip install -r requirements.txt
# load your .env (e.g. `export $(cat .env | xargs)` on Linux/Mac)
python monitor.py
```

Then schedule it:
- **Linux/VM (cron):** `crontab -e` and add:
  ```
  0,30 * * * * cd /path/to/palworld-ocg-monitor && /usr/bin/python3 monitor.py >> monitor.log 2>&1
  ```
- **Windows:** use Task Scheduler, trigger "daily, repeat every 30 minutes."

This needs the machine to actually be on at :00/:30 — fine for a VM,
not fine if it's a laptop that sleeps.

### Option B — GitHub Actions (free, no server to maintain)

1. Push this folder to a new GitHub repo (can be private).
2. Add the 4 notification values as repo secrets (see above).
3. That's it — `.github/workflows/monitor.yml` already runs it on the
   :00/:30 schedule and commits `state.json` back to the repo so it
   remembers what it's already alerted you about.
4. You can also trigger a manual run from the repo's Actions tab to test.

Free minutes comfortably cover a 30-min-interval job.

## Notes / limitations

- If a site uses aggressive bot protection (Cloudflare challenge, etc.),
  plain `requests` will get blocked — that's a case-by-case fix, not
  something solvable generically.
- The `html` parser only sees what's in the initial page load. JS-rendered
  storefronts need Playwright instead — say the word if one of your 8
  sites needs that and I'll add it.
- `state.json` is how it avoids double-emailing you — don't delete it
  unless you want a fresh (silent) baseline; deleting it does NOT send
  an "everything looks new" email, since the first run after a reset is
  intentionally silent.
