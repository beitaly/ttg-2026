"""
TTG 2026 — Best Holidays in Italy
Standalone Buyer Scrape Script v3

Phase 1: scrapes all buyer IDs, names and segments from listing pages
Phase 2: visits each buyer's diary page to extract full profile details
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
                            for b in all_buyers:
                                if b["id"] == buyer_id and seg_label not in b["segments"]:
                                    b["segments"] += f", {seg_label}"
                                    break
                            continue
                        seen_ids.add(buyer_id)
                        seg_new += 1
                        diary_url = BASE_URL + href if href.startswith("/") else href
                        all_buyers.append({
                            "id":        buyer_id,
                            "company":   company,
                            "segments":  seg_label,
                            "diary_url": diary_url,
                            "country":   "",
                            "contact":   "",
                            "website":   "",
                            "address":   "",
                        })
                    except Exception:
                        pass

                # Pagination
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

        log.info(f"Segment '{seg_label}' complete: {seg_new} buyers")

    log.info(f"Total unique buyers: {len(all_buyers)}")
    return all_buyers

# ── Phase 2: profile enrichment ───────────────────────────────────────────────

async def enrich_profiles(page, buyers):
    total = len(buyers)
    for i, buyer in enumerate(buyers):
        # Periodic re-login
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

            # Extract all data via JS to avoid selector fragility
            data = await page.evaluate("""() => {
                var result = {contact: '', country: '', website: '', address: ''};

                // Contact: "Buyer attending: Name"
                var header = document.querySelector('p.buyer-attending, span.buyer-attending, .scheda-buyer p');
                if (!header) header = document.querySelector('header p, .profile-header p');
                if (header) {
                    result.contact = header.innerText.replace(/Buyer attending:\\s*/i, '').trim();
                }

                // Website: first external link in profile area
                var links = document.querySelectorAll('a[href^="http"]');
                for (var l of links) {
                    var href = l.getAttribute('href') || '';
                    if (href && !href.includes('bme.iegexpo') && !href.includes('javascript')) {
                        result.website = href;
                        break;
                    }
                }

                // Address & country: from location/address block
                var addrEl = document.querySelector('address, .buyer-address, .user-details');
                if (!addrEl) addrEl = document.querySelector('.scheda-buyer address, section address');
                if (addrEl) {
                    var txt = addrEl.innerText.trim();
                    var lines = txt.split('\\n').map(l => l.trim()).filter(l => l);
                    result.address = lines.join(', ');
                    // Last line is usually the country
                    if (lines.length > 0) result.country = lines[lines.length - 1];
                }

                // Fallback for contact from any "Buyer attending" text
                if (!result.contact) {
                    var all = document.querySelectorAll('p, span, div');
                    for (var el of all) {
                        if (el.children.length === 0 && /buyer attending/i.test(el.innerText)) {
                            result.contact = el.innerText.replace(/Buyer attending:\\s*/i, '').trim();
                            break;
                        }
                    }
                }

                return result;
            }""")

            buyer["contact"] = data.get("contact", "")
            buyer["country"] = data.get("country", "")
            buyer["website"] = data.get("website", "")
            buyer["address"] = data.get("address", "")

            log.info(f"BUYER_DETAIL|{buyer['id']}|{buyer['company']}|{buyer['country']}|{buyer['segments']}|{buyer['contact']}|||{buyer['website']}|{buyer['address']}")

        except Exception as e:
            log.warning(f"  [{i+1}/{total}] Failed for {buyer['company']}: {e}")

        await asyncio.sleep(0.5)

    return buyers

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    log.info(f"BHI Full Scrape v3 started at {datetime.now(CET).isoformat()}")

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

        # Phase 1
        buyers = await scrape_listings(page)

        # Phase 2
        log.info(f"Enriching {len(buyers)} buyer profiles...")
        buyers = await enrich_profiles(page, buyers)

        await browser.close()

    # Write CSV
    if buyers:
        fieldnames = ["id", "company", "country", "segments", "contact", "website", "address", "diary_url"]
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(buyers)
        log.info(f"CSV written: {OUTPUT_CSV} ({len(buyers)} rows)")
    else:
        log.warning("No buyers found — CSV not written")

    log.info("Full scrape complete — exiting")


if __name__ == "__main__":
    asyncio.run(main())
