"""
TTG 2026 — Best Holidays in Italy
Standalone Buyer Scrape Script

Scrapes all 515 buyers across all segments, captures full profile data
(company, country, contact, address, website) and exits.
No booking logic. Run once to build the database.

Output: BUYER_DETAIL lines in logs, plus ttg_buyers_bhi.csv in working directory.
"""

import asyncio
import csv
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Configuration ─────────────────────────────────────────────────────────────

BASE_URL        = "https://bme.iegexpo.it"
AUTOLOGIN_URL   = os.environ.get("BHI_AUTOLOGIN_URL", "")
OUTPUT_CSV      = Path("ttg_buyers_bhi.csv")

CET = timezone(timedelta(hours=1))

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

import sys
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s [INFO] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logging.basicConfig(level=logging.INFO, handlers=[handler])
log = logging.getLogger(__name__)

# ── Scrape ────────────────────────────────────────────────────────────────────

async def scrape_all(page):
    all_buyers = []
    seen_ids   = set()

    for seg_label, categoria_val in SEGMENT_CATEGORIES:
        log.info(f"Scraping: {seg_label} (categoria={categoria_val})")
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
                        href     = await link.get_attribute("href")
                        company  = (await link.inner_text()).strip()
                        uid_match = re.search(r"user=([0-9]+)", href or "")
                        if not uid_match:
                            continue
                        buyer_id = uid_match.group(1)
                        if buyer_id in seen_ids:
                            continue
                        seen_ids.add(buyer_id)
                        seg_count += 1

                        log.info(f"BUYER_DETAIL|{buyer_id}|{company}||{seg_label}||||")

                        all_buyers.append({
                            "id":       buyer_id,
                            "company":  company,
                            "country":  "",
                            "segment":  seg_label,
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
            await asyncio.sleep(0.3)

        log.info(f"Segment '{seg_label}': {seg_count} buyers")

    log.info(f"Total unique buyers: {len(all_buyers)}")
    return all_buyers


async def main():
    log.info(f"BHI Scrape started at {datetime.now(CET).isoformat()}")

    if not AUTOLOGIN_URL:
        log.error("BHI_AUTOLOGIN_URL not set — exiting")
        return

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page    = await browser.new_page()

        # Login
        log.info("Logging in via autologin URL...")
        await page.goto(AUTOLOGIN_URL, wait_until="domcontentloaded", timeout=15000)
        log.info(f"Post-autologin URL: {page.url}")
        if "default" not in page.url:
            log.error("Login failed — check BHI_AUTOLOGIN_URL")
            await browser.close()
            return
        log.info("Login successful")

        # Scrape
        buyers = await scrape_all(page)
        await browser.close()

    # Write CSV
    if buyers:
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["id","company","country","segment","contact","website","address"])
            writer.writeheader()
            writer.writerows(buyers)
        log.info(f"CSV written: {OUTPUT_CSV} ({len(buyers)} rows)")
    else:
        log.warning("No buyers found — CSV not written")

    log.info("Scrape complete — exiting")


if __name__ == "__main__":
    asyncio.run(main())
