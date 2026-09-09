"""
TTG 2026 — Best Holidays in Italy
Appointment Request Automation Script v2
Rebuilt with confirmed buyer search page structure.

Navigation: alphabetical index (?ragione_sociale_iniziale=A...Z)
Segment prioritisation: uses categoria filter to scrape by type
Runs until Oct 8 10:00 CET, daily waves, variant cycling.
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

# ── Configuration ─────────────────────────────────────────────────────────────

BASE_URL  = "https://bme.iegexpo.it"
LOGIN_URL = "https://bme.iegexpo.it/ttg26/en/login"
BUYER_URL = f"{BASE_URL}/ttg26/en/ricerca-buyer"

CREDENTIALS = {
    "email":    os.environ.get("BHI_EMAIL", "info@bestholidaysinitaly.com"),
    "password": os.environ.get("BHI_PASSWORD", ""),
}

CET = timezone(timedelta(hours=1))
WINDOW_OPEN  = datetime(2026, 9,  8, 16, 0, 0, tzinfo=CET)
WINDOW_CLOSE = datetime(2026, 10, 8, 10, 0, 0, tzinfo=CET)
WAVE_INTERVAL_HOURS = 1

LOG_FILE = Path("ttg_bhi_log.csv")

# ── Segment priority & category IDs ──────────────────────────────────────────
# categoria field values from the buyer search form
# Script scrapes each segment separately so we can assign the right message
SEGMENT_CATEGORIES = [
    # (segment_label, categoria_value, message_variant_index)
    ("Tour Operator",         "4416957", 0),   # → Variant 1
    ("Luxury Travel Advisor", "26872093", 1),  # → Variant 2
    ("Incentive House",       "4416956",  2),  # → Variant 3
    ("Travel Agency",         "4416943",  0),  # → Variant 1 (general TO approach)
    ("Wholesaler",            "4416945",  3),  # → Variant 4
    ("OLTA/OTA",              "4416947",  3),  # → Variant 4
    ("PCO",                   "4416955",  2),  # → Variant 3 (MICE)
]

# Alphabetical index letters
LETTERS = list("123ABCDEFGHIJKLMNOPQRSTUVWXYZ")
TARGETS_CSV_URL = "https://raw.githubusercontent.com/beitaly/ttg-2026/main/TTG%20BHI%20Targets%20-%20Sheet1.csv"

# Priority countries
PRIORITY_COUNTRIES = {
    "France", "United Kingdom", "United States", "Canada",
    "Australia", "Germany", "Spain", "Brazil"
}

# ── Message variants ──────────────────────────────────────────────────────────
MESSAGES = [
    # 0 — International Tour Operators
    (
        "Dear {name}, Best Holidays in Italy is a specialist DMC operating across Puglia, Sicily, "
        "the Northern Lakes and beyond — FIT and groups, deluxe and first class. We work with "
        "international tour operators who want a knowledgeable, reliable Italian partner rather "
        "than just a ground handler. I'd love to explore how we might support your Italy programme. "
        "Looking forward to meeting at TTG. Concezio Natale, CEO, Best Holidays in Italy."
    ),
    # 1 — Luxury & Boutique Agents
    (
        "Dear {name}, Best Holidays in Italy crafts bespoke Italian experiences for discerning "
        "travellers — private villa stays, masseria immersions in Puglia, cultural journeys through "
        "Sicily and literary slow-travel routes. If your clients are looking for an Italy beyond "
        "the obvious, I think we have a lot to talk about. Looking forward to meeting at TTG. "
        "Concezio Natale, CEO, Best Holidays in Italy."
    ),
    # 2 — MICE & Corporate
    (
        "Dear {name}, Best Holidays in Italy specialises in exclusive group programmes across "
        "Southern Italy — full masseria and villa buyouts in Puglia, bespoke incentive itineraries "
        "and private dining experiences. A distinctive alternative to overworked Tuscany and Amalfi "
        "routes. I'd welcome the chance to discuss your group programme needs at TTG. "
        "Concezio Natale, CEO, Best Holidays in Italy."
    ),
    # 3 — Incoming Agents & Wholesalers
    (
        "Dear {name}, Best Holidays in Italy is a specialist Italian DMC covering Puglia, Sicily, "
        "the Northern Lakes and beyond — FIT ground arrangements, group programmes and villa stays "
        "across the country. Reliable, responsive and competitively structured. I'd be glad to "
        "discuss how we can support your Italy product at TTG. "
        "Concezio Natale, CEO, Best Holidays in Italy."
    ),
]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("ttg_bhi_script.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("BHI")


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


# ── Login ─────────────────────────────────────────────────────────────────────
async def login(page):
    log.info("Logging in via autologin URL...")
    autologin_url = os.environ.get("BHI_AUTOLOGIN_URL", "")
    if not autologin_url:
        raise Exception("BHI_AUTOLOGIN_URL environment variable not set")
    await page.goto(autologin_url, wait_until="networkidle", timeout=30000)
    log.info(f"Post-autologin URL: {page.url}")
    if "login" in page.url and "autologin" not in page.url:
        raise Exception(f"Autologin failed — still on login page: {page.url}")
    log.info("Login successful")


def fetch_targets(csv_url):
    """
    Fetch the target CSV from GitHub. Returns:
    - None  → sheet is empty or unreachable, target ALL buyers
    - set() → only target buyer IDs in this set (marked YES)
    """
    import urllib.request, csv, io
    try:
        with urllib.request.urlopen(csv_url, timeout=10) as r:
            content = r.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
        if not rows:
            log.info("Target sheet empty — targeting all buyers")
            return None
        targets = {
            row["Buyer ID"].strip()
            for row in rows
            if row.get("Target", "").strip().upper() == "YES"
        }
        log.info(f"Target sheet loaded: {len(targets)} buyers marked YES")
        return targets
    except Exception as e:
        log.warning(f"Could not fetch target sheet: {e} — targeting all buyers")
        return None

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

                        # Fetch buyer profile page for extra details
                        contact, website, description, markets = "", "", "", ""
                        try:
                            profile_url = BASE_URL + f"/ttg26/en/scheda-buyer?user={buyer_id}"
                            await page.goto(profile_url, wait_until="domcontentloaded", timeout=12000)
                            for sel in ["h2.nome", ".referente", "h3.nome", ".contact-name"]:
                                el = await page.query_selector(sel)
                                if el:
                                    contact = (await el.inner_text()).strip()
                                    break
                            for sel in ["a.sito-web", ".website a"]:
                                el = await page.query_selector(sel)
                                if el:
                                    website = (await el.get_attribute("href") or "").strip()
                                    if website:
                                        break
                            for sel in [".descrizione p", ".description p", ".profilo p"]:
                                el = await page.query_selector(sel)
                                if el:
                                    description = (await el.inner_text()).strip()[:300]
                                    if description:
                                        break
                            await page.goto(url, wait_until="domcontentloaded", timeout=12000)
                        except Exception:
                            try:
                                await page.goto(url, wait_until="domcontentloaded", timeout=12000)
                            except Exception:
                                pass

                        log.info(f"BUYER_DETAIL|{buyer_id}|{company}|{country}|{seg_label}|{contact}|{website}|{description[:80]}")
                        all_buyers.append({
                            "id": buyer_id, "name": company, "company": company,
                            "country": country,
                            "contact": contact, "website": website,
                            "description": description,
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
        await page.goto(target, wait_until="domcontentloaded", timeout=25000)

        # Wait for FullCalendar — increase to 20s, many pages are slow
        try:
            await page.wait_for_selector("div.fc-event", timeout=20000)
        except PlaywrightTimeout:
            # One retry with a fresh navigation
            await page.goto(target, wait_until="domcontentloaded", timeout=25000)
            try:
                await page.wait_for_selector("div.fc-event", timeout=15000)
            except PlaywrightTimeout:
                return "no_calendar"

        # Find free slot
        free_slot = await page.query_selector("div.fc-event.stato-libero")
        if not free_slot:
            return "no_free_slot"

        await free_slot.click()

        # Wait for modal
        try:
            await page.wait_for_selector("div.modal-dialog", timeout=8000)
        except PlaywrightTimeout:
            await free_slot.click()
            try:
                await page.wait_for_selector("div.modal-dialog", timeout=5000)
            except PlaywrightTimeout:
                return "no_modal"

        # Fill message
        msg_area = await page.query_selector("textarea[name='msg']")
        if msg_area:
            await msg_area.fill(message_text)

        # Submit
        submit = await page.query_selector(
            "button[data-action='/ttg26/en/richiedi-appuntamento-ajax']"
        )
        if submit:
            await submit.click()
            await page.wait_for_timeout(1000)
            return "sent"

        return "no_submit"

    except PlaywrightTimeout:
        return "timeout"
    except Exception as e:
        log.error(f"Error for {buyer.get('company','?')}: {e}")
        return "error"


async def run_wave(page, buyers, wave_number):
    """Cycle through message variants, refreshing login every 50 buyers."""
    wave_shift = (wave_number - 1) % len(MESSAGES)
    # Load target list from GitHub sheet
    targets = fetch_targets(TARGETS_CSV_URL)
    if targets is not None:
        buyers = [b for b in buyers if b["id"] in targets]
        log.info(f"After target filter: {len(buyers)} buyers to contact this wave")
    log.info(f"=== WAVE {wave_number} | shift={wave_shift} | {len(buyers)} buyers ===")
    no_calendar_streak = 0

    for i, buyer in enumerate(buyers):
        # Re-login every 50 buyers to prevent session expiry
        if i > 0 and i % 50 == 0:
            log.info(f"Session refresh at buyer {i+1}...")
            try:
                await login(page)
                no_calendar_streak = 0
                log.info("Session refreshed")
            except Exception as e:
                log.error(f"Session refresh failed: {e}")

        # Also re-login if we hit 5 consecutive no_calendar results
        if no_calendar_streak >= 5:
            log.info(f"Session likely expired (streak={no_calendar_streak}), re-logging in...")
            try:
                await login(page)
                no_calendar_streak = 0
                log.info("Session refreshed")
            except Exception as e:
                log.error(f"Session refresh failed: {e}")

        variant_idx  = (buyer["msg_variant"] + wave_shift) % len(MESSAGES)
        msg_template = MESSAGES[variant_idx]
        first_name   = (buyer["name"] or buyer["company"] or "there").split()[0]
        message_text = msg_template.format(name=first_name)

        status = await send_request(page, buyer, message_text)

        if status == "no_calendar":
            no_calendar_streak += 1
        else:
            no_calendar_streak = 0

        write_log(
            wave=wave_number, variant_idx=variant_idx, segment=buyer["segment"],
            buyer_id=buyer["id"], buyer_name=buyer["name"],
            buyer_company=buyer["company"], buyer_country=buyer["country"],
            status=status,
        )
        log.info(f"  [{i+1}/{len(buyers)}] {buyer['company']} ({buyer['segment']}) → {status}")
        await asyncio.sleep(1.5)

    log.info(f"=== Wave {wave_number} complete ===")


async def main():
    init_log()
    now = datetime.now(CET)
    log.info(f"BHI script started at {now.isoformat()}")

    if now < WINDOW_OPEN:
        wait_secs = (WINDOW_OPEN - now).total_seconds()
        log.info(f"Waiting {wait_secs:.0f}s until window opens at 16:00 CET...")
        await asyncio.sleep(wait_secs)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page    = await context.new_page()

        await login(page)
        buyers = await scrape_buyers_by_segment(page)

        if not buyers:
            log.error("No buyers found. Share the result page source after clicking a letter.")
            await browser.close()
            return

        wave = 1
        while datetime.now(CET) < WINDOW_CLOSE:
            wave_start = datetime.now(CET)
            log.info(f"Starting wave {wave} at {wave_start.isoformat()}")

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
                log.info("Next wave after close — done.")
                break
            wait_secs = (next_wave - datetime.now(CET)).total_seconds()
            if wait_secs > 0:
                log.info(f"Next wave in {wait_secs/3600:.1f}h...")
                await asyncio.sleep(wait_secs)

        log.info("Window closed. BHI script complete.")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
