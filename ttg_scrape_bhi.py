"""
TTG 2026 — Best Holidays in Italy
Standalone Buyer Scrape Script v4

Phase 1: scrapes all buyer IDs, names from listing pages
Phase 2: visits each buyer's profile page to extract full details
Output: ttg_buyers_full.csv
"""

import asyncio
import csv
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL      = "https://bme.iegexpo.it"
AUTOLOGIN_URL = os.environ.get("BHI_AUTOLOGIN_URL", "")
OUTPUT_CSV    = Path("ttg_buyers_full.csv")
CET           = timezone(timedelta(hours=1))

SEGMENT_CATEGORIES = [
    ("Tour Operator",   "4416957"),
    ("Incentive House", "4416956"),
    ("Travel Agency",   "4416943"),
    ("Wholesaler",      "4416945"),
    ("OLTA/OTA",        "4416947"),
    ("PCO",             "4416955"),
]

LETTERS = list("123ABCDEFGHIJKLMNOPQRSTUVWXYZ")

# ── Logging ───────────────────────────────────────────────────────────────────

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s [INFO] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logging.basicConfig(level=logging.INFO, handlers=[handler])
log = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

async def dismiss_cookie_banner(page):
    try:
        btn = await page.query_selector("button#c-p-bn, button.c-bn[data-role='acceptAll'], button:has-text('ACCEPT ALL COOKIES')")
        if btn:
            await btn.click(timeout=2000)
            await asyncio.sleep(0.3)
    except Exception:
        pass
    try:
        await page.evaluate("document.getElementById('cc--main') && document.getElementById('cc--main').remove()")
    except Exception:
        pass

async def login(page):
    await page.goto(AUTOLOGIN_URL, wait_until="domcontentloaded", timeout=15000)
    if "default" not in page.url:
        raise RuntimeError(f"Login failed — URL: {page.url}")
    await dismiss_cookie_banner(page)
    log.info("Login successful")

# ── Phase 1: listing pages ────────────────────────────────────────────────────

async def scrape_listings(page):
    all_buyers = []
    seen_ids   = set()

    for seg_label, categoria_val in SEGMENT_CATEGORIES:
        log.info(f"Listing: {seg_label} (categoria={categoria_val})")
        seg_new = 0

        for letter in LETTERS:
            page_num = 1
            while True:
                url = (f"{BASE_URL}/ttg26/en/ricerca-buyer"
                       f"?ragione_sociale_iniziale={letter}"
                       f"&categoria={categoria_val}&submit=1&page={page_num}")
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=12000)
                except Exception as e:
                    log.warning(f"  Load failed {letter} p{page_num}: {e}")
                    break

                entries = await page.query_selector_all("li.search-result")
                if not entries:
                    break

                for entry in entries:
                    try:
                        link = await entry.query_selector("h4 a[href*='agenda-appuntamenti']")
                        if not link:
                            continue
                        href     = await link.get_attribute("href")
                        company  = (await link.inner_text()).strip()
                        m        = re.search(r"user=([0-9]+)", href or "")
                        if not m:
                            continue
                        buyer_id = m.group(1)
                        if buyer_id in seen_ids:
                            continue
                        seen_ids.add(buyer_id)
                        seg_new += 1
                        diary_url = BASE_URL + href if href.startswith("/") else href
                        all_buyers.append({
                            "id":                    buyer_id,
                            "company":               company,
                            "diary_url":             diary_url,
                            "contact":               "",
                            "country":               "",
                            "website":               "",
                            "address":               "",
                            "type_of_business":      "",
                            "product_category":      "",
                            "sector":                "",
                            "type_of_product":       "",
                            "type_of_booking":       "",
                            "trade_sector":          "",
                            "activities_services":   "",
                            "geo_areas":             "",
                            "destinations":          "",
                            "market_segments":       "",
                            "business_volume":       "",
                            "yearly_visitors":       "",
                            "employees":             "",
                            "years_operating":       "",
                            "exhibitions":           "",
                        })
                    except Exception:
                        pass

                next_btn = await page.query_selector("ul.pagination li.last a")
                if next_btn:
                    next_href = await next_btn.get_attribute("href") or ""
                    if f"page={page_num}" in next_href:
                        break
                    page_num += 1
                else:
                    break
                await asyncio.sleep(0.2)

            log.info(f"  {letter}: {seg_new} in {seg_label}")
            await asyncio.sleep(0.2)

        log.info(f"Segment '{seg_label}' complete")

    log.info(f"Total unique buyers: {len(all_buyers)}")
    return all_buyers

# ── Phase 2: profile enrichment ───────────────────────────────────────────────

async def enrich_profiles(page, buyers):
    total = len(buyers)
    for i, buyer in enumerate(buyers):
        if i > 0 and i % 50 == 0:
            log.info(f"  [{i}/{total}] Re-logging in...")
            try:
                await login(page)
            except Exception as e:
                log.warning(f"  Re-login failed: {e}")

        profile_url = f"{BASE_URL}/ttg26/en/agenda-appuntamenti?user={buyer['id']}"
        try:
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=12000)
            await dismiss_cookie_banner(page)

            data = await page.evaluate("""() => {
                var result = {};

                // Contact name
                var contactEl = document.querySelector('p.buyer-attending, .buyer-attending');
                if (!contactEl) {
                    // Look for "Buyer attending:" text
                    var allP = document.querySelectorAll('p, span');
                    for (var el of allP) {
                        if (el.children.length === 0 && /buyer attending/i.test(el.innerText)) {
                            contactEl = el; break;
                        }
                    }
                }
                result.contact = contactEl
                    ? contactEl.innerText.replace(/Buyer attending:\\s*/i, '').trim()
                    : '';

                // Website
                var links = document.querySelectorAll('a[href^="http"], a[href^="www"]');
                for (var l of links) {
                    var href = l.getAttribute('href') || '';
                    if (href && !href.includes('bme.iegexpo') && !href.includes('javascript')
                        && !href.includes('cookieconsent') && href.length > 5) {
                        result.website = href;
                        break;
                    }
                }

                // Address block
                var addrEl = document.querySelector('address, .buyer-address');
                if (!addrEl) {
                    // Try finding the location pin icon area
                    var icon = document.querySelector('i.fa-map-marker, i.fa-location, .glyphicon-map-marker');
                    if (icon) addrEl = icon.closest('li, div, p');
                }
                if (addrEl) {
                    var lines = addrEl.innerText.trim().split('\\n').map(l => l.trim()).filter(l => l);
                    result.address = lines.join(', ');
                    result.country = lines.length > 0 ? lines[lines.length - 1] : '';
                }

                // Profile table — extract all key/value pairs
                var profileData = {};
                var rows = document.querySelectorAll('.profile table tr, table.profile tr, .scheda-buyer table tr');
                if (!rows.length) rows = document.querySelectorAll('table tr');
                rows.forEach(function(row) {
                    var cells = row.querySelectorAll('td, th');
                    if (cells.length >= 2) {
                        var key = cells[0].innerText.trim().toLowerCase();
                        var val = cells[1].innerText.trim();
                        profileData[key] = val;
                    }
                });

                // Also try dt/dd pairs
                var dts = document.querySelectorAll('dt');
                dts.forEach(function(dt) {
                    var dd = dt.nextElementSibling;
                    if (dd && dd.tagName === 'DD') {
                        profileData[dt.innerText.trim().toLowerCase()] = dd.innerText.trim();
                    }
                });

                result.profileData = profileData;
                return result;
            }""")

            buyer["contact"] = data.get("contact", "")
            buyer["website"] = data.get("website", "")
            buyer["address"] = data.get("address", "")
            buyer["country"] = data.get("country", "")

            # Map profile fields
            pd = data.get("profileData", {})
            def get(keys):
                for k in keys:
                    for pk, pv in pd.items():
                        if k.lower() in pk:
                            return pv
                return ""

            buyer["type_of_business"]    = get(["type of business"])
            buyer["product_category"]    = get(["product category"])
            buyer["sector"]              = get(["sector"])
            buyer["type_of_product"]     = get(["type of product"])
            buyer["type_of_booking"]     = get(["type of booking"])
            buyer["trade_sector"]        = get(["requested trade sector", "trade sector"])
            buyer["activities_services"] = get(["requested activities", "activities and services"])
            buyer["geo_areas"]           = get(["geographical areas are included", "geographical areas of interest"])
            buyer["destinations"]        = get(["destinations of interest", "destination"])
            buyer["market_segments"]     = get(["market segments"])
            buyer["business_volume"]     = get(["volume of your business", "business volume"])
            buyer["yearly_visitors"]     = get(["yearly visitors", "visitors to italy"])
            buyer["employees"]           = get(["number of employees"])
            buyer["years_operating"]     = get(["years has your company"])
            buyer["exhibitions"]         = get(["tourism exhibitions"])

            log.info(f"[{i+1}/{total}] {buyer['company']} | {buyer['country']} | {buyer['trade_sector'] or buyer['type_of_business']}")

        except Exception as e:
            log.warning(f"  [{i+1}/{total}] Failed for {buyer['company']}: {e}")

        await asyncio.sleep(0.5)

    return buyers

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    log.info(f"BHI Full Scrape v4 started at {datetime.now(CET).isoformat()}")

    if not AUTOLOGIN_URL:
        log.error("BHI_AUTOLOGIN_URL not set — exiting")
        return

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page    = await browser.new_page()

        log.info("Logging in...")
        try:
            await login(page)
        except Exception as e:
            log.error(f"Login failed: {e}")
            await browser.close()
            return

        buyers = await scrape_listings(page)

        log.info(f"Enriching {len(buyers)} buyer profiles...")
        buyers = await enrich_profiles(page, buyers)

        await browser.close()

    if buyers:
        fieldnames = [
            "id", "company", "country", "contact", "website", "address",
            "trade_sector", "type_of_business", "product_category", "sector",
            "type_of_product", "type_of_booking", "activities_services",
            "geo_areas", "destinations", "market_segments",
            "business_volume", "yearly_visitors", "employees",
            "years_operating", "exhibitions", "diary_url"
        ]
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(buyers)
        log.info(f"CSV written: {OUTPUT_CSV} ({len(buyers)} rows)")
    else:
        log.warning("No buyers found — CSV not written")

    log.info("Full scrape complete — exiting")


if __name__ == "__main__":
    asyncio.run(main())
