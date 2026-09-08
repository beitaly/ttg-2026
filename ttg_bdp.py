"""
TTG 2026 — Borracce di Poesia
Appointment Request Automation Script v2
Same structure as BHI v2 but with BDP credentials and messages.
"""

import asyncio
import sys
import csv
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

BASE_URL  = "https://bme.iegexpo.it"
LOGIN_URL = "https://bme.iegexpo.it/ttg26/en/login"
BUYER_URL = f"{BASE_URL}/ttg26/en/ricerca-buyer"

CREDENTIALS = {
    "email":    os.environ.get("BDP_EMAIL", ""),
    "password": os.environ.get("BDP_PASSWORD", ""),
}

CET = timezone(timedelta(hours=1))
WINDOW_OPEN  = datetime(2026, 9,  8, 16, 0, 0, tzinfo=CET)
WINDOW_CLOSE = datetime(2026, 10, 8, 10, 0, 0, tzinfo=CET)
WAVE_INTERVAL_HOURS = 24

LOG_FILE = Path("ttg_bdp_log.csv")

# BDP targets cultural/boutique agents first, then outdoor/active, then general TOs
SEGMENT_CATEGORIES = [
    ("Luxury Travel Advisor", "26872093", 0),  # → Variant 1 (cultural/boutique)
    ("Travel Agency",         "4416943",  2),  # → Variant 3 (general TO)
    ("Tour Operator",         "4416957",  2),  # → Variant 3 (general TO)
    ("Wholesaler",            "4416945",  2),  # → Variant 3
    ("Incentive House",       "4416956",  1),  # → Variant 2 (active/outdoor)
]

LETTERS = list("123ABCDEFGHIJKLMNOPQRSTUVWXYZ")

MESSAGES = [
    # 0 — Boutique & Cultural Agents
    (
        "Dear {name}, Borracce di Poesia — Bidons of Poetry — offers a truly unique experience: "
        "guided e-bike tours through Abruzzo, Puglia, Marche and Umbria where local history, art "
        "and original poetry bring the landscape to life. Guests even participate in a live "
        "cyclopoetic workshop, creating their own verses on the road. For travellers seeking "
        "meaning over miles. Looking forward to meeting at TTG. "
        "Alessandro Ricci, Borracce di Poesia."
    ),
    # 1 — Active & Outdoor Specialists
    (
        "Dear {name}, Borracce di Poesia offers e-bike tours across Central and Southern Italy — "
        "Abruzzo, Puglia, Marche and Umbria — designed for all ages and abilities, from "
        "first-timers to experienced cyclists. One-day excursions and multi-day itineraries for "
        "individuals, couples, groups and families. Slow travel at its most accessible. "
        "I'd welcome the chance to discuss at TTG. "
        "Alessandro Ricci, Borracce di Poesia."
    ),
    # 2 — General TOs
    (
        "Dear {name}, Borracce di Poesia is a specialist slow-travel operator offering e-bike "
        "tours across Italy's most authentic regions — Abruzzo, Puglia, Marche and Umbria. "
        "Bespoke FIT and group itineraries combining cycling, storytelling, local culture and "
        "original poetry. A genuinely distinctive product for clients looking beyond the "
        "mainstream. Looking forward to meeting at TTG. "
        "Alessandro Ricci, Borracce di Poesia."
    ),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("ttg_bdp_script.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("BDP")


def init_log():
    if not LOG_FILE.exists():
        with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                "timestamp", "wave", "variant_index", "segment",
                "buyer_id", "buyer_name", "buyer_company", "buyer_country",
                "status", "notes"
            ])


def write_log(wave, variant_idx, segment, buyer_id, buyer_name,
              buyer_company, buyer_country, status, notes=""):
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            datetime.now(CET).isoformat(),
            wave, variant_idx, segment,
            buyer_id, buyer_name, buyer_company, buyer_country,
            status, notes
        ])


async def login(page):
    log.info("Logging in via autologin URL...")
    autologin_url = os.environ.get("BDP_AUTOLOGIN_URL", "")
    if not autologin_url:
        raise Exception("BDP_AUTOLOGIN_URL environment variable not set")
    await page.goto(autologin_url, wait_until="networkidle", timeout=30000)
    log.info(f"Post-autologin URL: {page.url}")
    if "login" in page.url and "autologin" not in page.url:
        raise Exception(f"Autologin failed — still on login page: {page.url}")
    log.info("Login successful")

async def scrape_buyers_by_segment(page):
    all_buyers = []
    seen_ids   = set()

    for seg_label, categoria_val, msg_idx in SEGMENT_CATEGORIES:
        log.info(f"Scraping: {seg_label} (categoria={categoria_val})")
        seg_count = 0

        for letter in LETTERS:
            page_num = 1
            while True:
                url = (f"{BASE_URL}/ttg26/en/ricerca-buyer"
                       f"?ragione_sociale_iniziale={letter}"
                       f"&categoria={categoria_val}&submit=1&page={page_num}")
                try:
                    await page.goto(url, wait_until="networkidle", timeout=20000)
                except Exception as e:
                    log.warning(f"  Timeout {letter} p{page_num}: {e}")
                    break

                # Confirmed selector from live page HTML
                entries = await page.query_selector_all("li.search-result")
                if not entries:
                    break

                for entry in entries:
                    try:
                        link = await entry.query_selector("h4 a[href*='agenda-appuntamenti']")
                        if not link:
                            continue
                        href    = await link.get_attribute("href")
                        company = (await link.inner_text()).strip()
                        # Extract user ID: /ttg26/en/agenda-appuntamenti?user=12345
                        uid_match = re.search("user=([0-9]+)", href or "")
                        buyer_id  = uid_match.group(1) if uid_match else ""
                        if not buyer_id or buyer_id in seen_ids:
                            continue
                        country_el = await entry.query_selector("p.risultati-info span")
                        country = (await country_el.inner_text()).strip() if country_el else ""
                        seen_ids.add(buyer_id)
                        seg_count += 1
                        all_buyers.append({
                            "id": buyer_id, "name": company, "company": company,
                            "country": country,
                            "appt_url": BASE_URL + href,
                            "segment": seg_label, "msg_variant": msg_idx,
                        })
                    except Exception as e:
                        log.debug(f"Entry parse error: {e}")

                # Pagination
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

    final, seen2 = [], set()
    for b in all_buyers:
        if b["id"] not in seen2:
            seen2.add(b["id"])
            final.append(b)
    log.info(f"Total unique buyers: {len(final)}")
    return final


async def send_request(page, buyer, message_text):
    try:
        target = buyer.get("appt_url") or (BASE_URL + "/ttg26/en/agenda-appuntamenti?user=" + buyer["id"])
        await page.goto(target, wait_until="domcontentloaded", timeout=20000)

        # Wait for FullCalendar to render — wait for ANY fc-event to appear
        try:
            await page.wait_for_selector("div.fc-event", timeout=12000)
        except PlaywrightTimeout:
            return "no_calendar"

        # Now look for a free slot
        free_slot = await page.query_selector("div.fc-event.stato-libero")
        if not free_slot:
            return "no_free_slot"

        await free_slot.click()

        # Wait for modal to appear
        try:
            await page.wait_for_selector("textarea, .fancybox-inner", timeout=5000)
        except PlaywrightTimeout:
            return "no_modal"

        # Fill message
        msg_area = await page.query_selector("textarea, .fancybox-inner textarea")
        if msg_area:
            await msg_area.fill(message_text)

        # Click submit
        submit = await page.query_selector(
            "button:has-text('Request an appointment'), "
            "button:has-text('Richiedi un appuntamento'), "
            "button.btn-primary"
        )
        if submit:
            await submit.click()
            await page.wait_for_timeout(800)
            return "sent"

        return "no_submit"

    except PlaywrightTimeout:
        return "timeout"
    except Exception as e:
        log.error(f"Error for {buyer.get('company','?')}: {e}")
        return "error"


async def run_wave(page, buyers, wave_number):
    wave_shift = (wave_number - 1) % len(MESSAGES)
    log.info(f"=== WAVE {wave_number} | {len(buyers)} buyers ===")

    for i, buyer in enumerate(buyers):
        variant_idx  = (buyer["msg_variant"] + wave_shift) % len(MESSAGES)
        msg_template = MESSAGES[variant_idx]
        first_name   = (buyer["name"] or buyer["company"] or "there").split()[0]
        message_text = msg_template.format(name=first_name)

        status = await send_request(page, buyer, message_text)
        write_log(wave_number, variant_idx, buyer["segment"],
                  buyer["id"], buyer["name"], buyer["company"],
                  buyer["country"], status)

        log.info(f"  [{i+1}/{len(buyers)}] {buyer['company']} → {status}")
        await asyncio.sleep(1.5)

    log.info(f"=== Wave {wave_number} complete ===")


async def main():
    init_log()
    now = datetime.now(CET)
    log.info(f"BDP script started at {now.isoformat()}")

    if now < WINDOW_OPEN:
        wait_secs = (WINDOW_OPEN - now).total_seconds()
        log.info(f"Waiting {wait_secs:.0f}s...")
        await asyncio.sleep(wait_secs)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await (await browser.new_context()).new_page()

        await login(page)
        buyers = await scrape_buyers_by_segment(page)

        if not buyers:
            log.error("No buyers found.")
            await browser.close()
            return

        wave = 1
        while datetime.now(CET) < WINDOW_CLOSE:
            wave_start = datetime.now(CET)
            try:
                if wave > 1:
                    buyers = await scrape_buyers_by_segment(page)
                await run_wave(page, buyers, wave)
            except Exception as e:
                log.error(f"Wave {wave} error: {e}")
                try:
                    await login(page)
                except Exception:
                    pass

            wave += 1
            next_wave = wave_start + timedelta(hours=WAVE_INTERVAL_HOURS)
            if next_wave >= WINDOW_CLOSE:
                break
            wait_secs = (next_wave - datetime.now(CET)).total_seconds()
            if wait_secs > 0:
                await asyncio.sleep(wait_secs)

        log.info("BDP script complete.")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
