# Palworld OCG Monitor

Checks your listed sites every 30 min for new/restocked Palworld OCG
products and texts you when something shows up.

## 1. Your sites (already filled in)

`sites_config.json` is configured for your 7 sites:

| Site | Type | Status |
|---|---|---|
| Flipside Gaming | Shopify | Ready |
| Topspot Cards | Shopify | Ready |
| VGMX | Shopify | Ready |
| Lumius Inc | Shopify | Ready |
| Gamers Getaway KC | Shopify | Ready |
| Moonlight Collectibles | Shopify | Ready |
| Game Nerdz | BigCommerce (`bigcommerce_bodl`) | Ready |

6 of the 7 run on Shopify, so they use the search-based `products.json`
approach with no scraping guesswork. Game Nerdz runs on BigCommerce with
a JS-rendered product grid (nothing usable in the plain HTML), so it
instead reads the analytics data blob BigCommerce embeds in the
server-rendered page - see `parse_bigcommerce_bodl()` in `monitor.py`.
One tradeoff: there's no per-product URL in that data (real product
slugs are only generated client-side), so Game Nerdz alerts link back to
the category page rather than the exact product.

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

Set `"enabled": true` on each site once configured. Send me the actual 8
URLs and I'll write the real selectors/configs for you instead of guessing.

## 2. Notifications (SMS + email)

Both channels are independent - if one fails (e.g. a Twilio account
issue), the other still goes out and the run still succeeds. It only
fails (which is what keeps `state.json` from advancing, so you don't
lose an alert) if **both** fail.

### SMS via Twilio

1. Create a Twilio account, buy a phone number (~$1/mo).
2. Grab your Account SID + Auth Token from the Twilio console.
3. Note: trial (unfunded) Twilio accounts can only send Twilio's own
   predefined message templates, not custom text — you'll need to fund
   the account before SMS alerts here will actually go through.

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
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=
TWILIO_TO_NUMBER=
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
2. Add the 4 Twilio values as repo secrets (see above).
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
- `state.json` is how it avoids double-texting you — don't delete it
  unless you want a fresh "everything looks new" alert on the next run.
