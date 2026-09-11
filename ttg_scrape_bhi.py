"""
TTG 2026 — Best Holidays in Italy
Standalone Buyer Scrape Script v2

Scrapes all buyers across all segments from listing pages,
then visits each individual profile page to collect full details:
company, country, contact, website, address, diary URL.

Output: ttg_buyers_full.csv in working directory.
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

# ── Configuration ─────────────────────────────────────────────────────────────

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

# ── Cookie banner ─────────────────────────────────────────────────────────────

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

# ── Phase 1: scrape listing pages ─────────────────────────────────────────────

async def scrape_listings(page):
    """Collect buyer IDs, names and segments from search listing pages."""
    all_buyers = []
    seen_ids   = set()

    for seg_label, categoria_val in SEGMENT_CATEGORIES:
        log.info(f"Listing: {seg_label} (categoria={categoria_val})")
        seg_count = 0

        for letter in LETTERS:
            page_num = 1
            while True:
                url = (f"{BASE_URL}/ttg26/en/ricerca-buyer"
                       f"?ragione_sociale_iniziale={letter}"
                       f"&categoria={categoria_val}&submit=1&page={page_num}")
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                except Exception as e:
                    log.warning(f"  Page load failed {letter} p{page_num}: {e}")
                    break

                entries = await page.query_selector_all("li.search-result")
                if not entries:
                    break

                for entry in entries:
                    try:
                        link = await entry.query_selector("h4 a[href*='agenda-appuntamenti']")
                        if not link:
                            continue
                        href      = await link.get_attribute("href")
                        company   = (await link.inner_text()).strip()
                        uid_match = re.search(r"user=([0-9]+)", href or "")
                        if not uid_match:
                            continue
                        buyer_id = uid_match.group(1)
                        if buyer_id in seen_ids:
                            # Add segment to existing buyer
                            for b in all_buyers:
                                if b["id"] == buyer_id:
                                    if seg_label not in b["segments"]:
                                        b["segments"] += f", {seg_label}"
                                    break
                            continue
                        seen_ids.add(buyer_id)
                        seg_count += 1
                        diary_url = BASE_URL + href if href.startswith("/") else href
                        all_buyers.append({
                            "id":       buyer_id,
                            "company":  company,
                            "segments": seg_label,
                            "diary_url": diary_url,
                            "country":  "",
                            "contact":  "",
                            "website":  "",
                            "address":  "",
                        })
                    except Exception as e:
                        log.debug(f"Entry parse error: {e}")

                next_link = await page.query_selector("ul.pagination li.last a")
                if next_link:
                    next_href = await next_link.get_attribute("href") or ""
                    if "page=" + str(page_num) in next_href:
                        break
                    page_num += 1
                else:
                    break
                await asyncio.sleep(0.3)

            log.info(f"  {letter}: {seg_count} in {seg_label}")
            await asyncio.sleep(0.2)

        log.info(f"Segment '{seg_label}': {seg_count} buyers")

    log.info(f"Total unique buyers from listings: {len(all_buyers)}")
    return all_buyers


# ── Phase 2: visit each profile page ─────────────────────────────────────────

async def enrich_profiles(page, buyers):
    """Visit each buyer's diary/profile page to extract full details."""
    total = len(buyers)
    for i, buyer in enumerate(buyers):
        if i > 0 and i % 50 == 0:
            log.info(f"  [{i}/{total}] Re-logging in...")
            try:
                await page.goto(AUTOLOGIN_URL, wait_until="domcontentloaded", timeout=15000)
                await dismiss_cookie_banner(page)
            except Exception as e:
                log.warning(f"  Re-login failed: {e}")

        profile_url = (BASE_URL + f"/ttg26/en/agenda-appuntamenti?user={buyer['id']}")
        try:
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=12000)
            await dismiss_cookie_banner(page)

            # Country — from location block
            loc = await page.query_selector("li div")
            country = ""
            if loc:
                loc_text = (await loc.inner_text()).strip()
                # Last non-empty line is usually country
                lines = [l.strip() for l in loc_text.split("\n") if l.strip()]
                if lines:
                    country = lines[-1]

            # Contact name — from span under header
            contact_el = await page.query_selector("header span")
            contact = ""
            if contact_el:
                contact = (await contact_el.inner_text()).strip()
                # Remove "Buyer attending: " prefix if present
                contact = re.sub(r"^Buyer attending:\s*", "", contact).strip()

            # Website
            website_el = await page.query_selector("a[href^='http']:not([href*='bme.iegexpo'])")
            website = ""
            if website_el:
                website = (await website_el.get_attribute("href") or "").strip()

            # Address — from user-details li div
            address_els = await page.query_selector_all("ul.user-details li div")
            address = ""
            for el in address_els:
                txt = (await el.inner_text()).strip()
                # Skip if it looks like a URL
                if txt and "http" not in txt and len(txt) > 5:
                    address = txt.replace("\n", ", ")
                    break

            buyer["country"] = country
            buyer["contact"] = contact
            buyer["website"] = website
            buyer["address"] = address

            log.info(f"BUYER_DETAIL|{buyer['id']}|{buyer['company']}|{country}|{buyer['segments']}|{contact}|||{website}|{address}")

        except Exception as e:
            log.warning(f"  [{i+1}/{total}] Profile fetch failed for {buyer['company']}: {e}")

        await asyncio.sleep(0.5)

    return buyers


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    log.info(f"BHI Full Scrape v2 started at {datetime.now(CET).isoformat()}")

    if not AUTOLOGIN_URL:
        log.error("BHI_AUTOLOGIN_URL not set — exiting")
        return

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page    = await browser.new_page()

        log.info("Logging in...")
        await page.goto(AUTOLOGIN_URL, wait_until="domcontentloaded", timeout=15000)
        log.info(f"Post-autologin URL: {page.url}")
        if "default" not in page.url:
            log.error("Login failed — check BHI_AUTOLOGIN_URL")
            await browser.close()
            return
        await dismiss_cookie_banner(page)
        log.info("Login successful")

        # Phase 1: listing pages
        buyers = await scrape_listings(page)

        # Phase 2: individual profile pages
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
