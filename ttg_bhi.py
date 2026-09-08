"""
TTG 2026 — Best Holidays in Italy
Appointment Request Automation Script v2
Rebuilt with confirmed buyer search page structure.

Navigation: alphabetical index (?ragione_sociale_iniziale=A...Z)
Segment prioritisation: uses categoria filter to scrape by type
Runs until Oct 8 10:00 CET, daily waves, variant cycling.
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

BASE_URL  = "https://www.ttgexpo.it"
LOGIN_URL = f"{BASE_URL}/ttg26/en/login"
BUYER_URL = f"{BASE_URL}/ttg26/en/ricerca-buyer"

# Credentials loaded from Railway environment variables (set in Railway dashboard)
CREDENTIALS = {
    "email":    os.environ.get("BHI_EMAIL", "info@bestholidaysinitaly.com"),
    "password": os.environ.get("BHI_PASSWORD", ""),
}

CET = timezone(timedelta(hours=1))
WINDOW_OPEN  = datetime(2026, 9,  8, 16, 0, 0, tzinfo=CET)
WINDOW_CLOSE = datetime(2026, 10, 8, 10, 0, 0, tzinfo=CET)
WAVE_INTERVAL_HOURS = 24

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
        logging.StreamHandler(),
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
    log.info("Navigating to login page...")
    try:
        await page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
        log.info(f"Login page loaded: {page.url}")
    except Exception as e:
        log.error(f"Failed to load login page: {e}")
        raise

    try:
        await page.click("button:has-text('ACCEPT ALL')", timeout=3000)
        log.info("Cookies accepted")
    except PlaywrightTimeout:
        log.info("No cookie banner found")

    # Log all inputs for debugging
    inputs = await page.query_selector_all("input")
    for inp in inputs:
        name = await inp.get_attribute("name")
        type_ = await inp.get_attribute("type")
        id_ = await inp.get_attribute("id")
        log.info(f"Input found: name={name} type={type_} id={id_}")

    # Try multiple email selectors
    for sel in ["input[name='userid']","input[name='email']","input[type='email']","input[name='username']","input[id='userid']"]:
        try:
            await page.fill(sel, CREDENTIALS["email"])
            log.info(f"Email filled: {sel}")
            break
        except Exception:
            continue
    else:
        log.error("Email field not found")
        raise Exception("Email field not found")

    # Try multiple password selectors
    for sel in ["input[name='password']","input[type='password']","input[id='password']"]:
        try:
            await page.fill(sel, CREDENTIALS["password"])
            log.info(f"Password filled: {sel}")
            break
        except Exception:
            continue
    else:
        log.error("Password field not found")
        raise Exception("Password field not found")

    # Submit
    for sel in ["button[type=\'submit\']","input[type=\'submit\']","button:has-text(\'Login\')","button:has-text(\'Accedi\')"]:
        try:
            await page.click(sel)
            log.info(f"Submit clicked: {sel}")
            break
        except Exception:
            continue

    try:
        await page.wait_for_url("**/default**", timeout=15000)
        log.info(f"Login successful: {page.url}")
    except PlaywrightTimeout:
        log.error(f"Login failed. URL: {page.url}")
        await page.screenshot(path="login_debug_bhi.png")
        raise


# ── Scrape buyers by segment ──────────────────────────────────────────────────
async def scrape_buyers_by_segment(page):
    """
    Scrape all buyers, organised by segment priority.
    Returns list of dicts with segment and message_variant pre-assigned.
    Deduplicates so each buyer appears only once (first/highest priority segment wins).
    """
    all_buyers = []
    seen_ids = set()

    for seg_label, categoria_val, msg_idx in SEGMENT_CATEGORIES:
        log.info(f"Scraping segment: {seg_label} (categoria={categoria_val})")
        seg_buyers = []

        for letter in LETTERS:
            url = (f"{BUYER_URL}?ragione_sociale_iniziale={letter}"
                   f"&categoria={categoria_val}&submit=1")
            try:
                await page.goto(url, wait_until="networkidle", timeout=20000)
            except PlaywrightTimeout:
                log.warning(f"  Timeout on {letter}, skipping")
                continue

            # Extract buyer cards/rows from results
            # The page shows results in a table or card list after search
            entries = await page.query_selector_all(
                "table.risultati tbody tr, "
                ".risultati .risultato, "
                ".search-results tr[data-id], "
                "tr.buyer-row, "
                ".scheda-breve"
            )

            if not entries:
                # Try generic table rows (skip header)
                entries = await page.query_selector_all("table tbody tr:not(:first-child)")

            for entry in entries:
                try:
                    # Get profile link — the key to identifying each buyer
                    link = await entry.query_selector(
                        "a[href*='/ttg26/en/ricerca-buyer/'], "
                        "a[href*='/scheda/'], "
                        "a.btn-appuntamento, "
                        "a[href*='slot']"
                    )
                    if not link:
                        # Try any link in the row
                        link = await entry.query_selector("a[href]")

                    href = await link.get_attribute("href") if link else ""
                    profile_url = (BASE_URL + href
                                   if href and href.startswith("/") else href or "")

                    # Extract buyer ID from URL
                    buyer_id = ""
                    m = re.search(r"/(\d+)/?(?:\?|$)", href or "")
                    if m:
                        buyer_id = m.group(1)

                    # Skip if already captured from higher priority segment
                    uid = buyer_id or profile_url
                    if not uid or uid in seen_ids:
                        continue

                    # Extract name, company, country from cells
                    cells = await entry.query_selector_all("td")
                    texts = []
                    for cell in cells:
                        t = (await cell.inner_text()).strip()
                        if t:
                            texts.append(t)

                    company = texts[0] if len(texts) > 0 else ""
                    country = texts[1] if len(texts) > 1 else ""
                    name    = texts[2] if len(texts) > 2 else company

                    # Also try named selectors
                    name_el = await entry.query_selector(".nome, .referente, .contact-name")
                    if name_el:
                        name = (await name_el.inner_text()).strip()

                    co_el = await entry.query_selector(".ragione-sociale, .company, .azienda")
                    if co_el:
                        company = (await co_el.inner_text()).strip()

                    seen_ids.add(uid)
                    seg_buyers.append({
                        "id":            buyer_id,
                        "name":          name,
                        "company":       company,
                        "country":       country,
                        "profile_url":   profile_url,
                        "segment":       seg_label,
                        "msg_variant":   msg_idx,
                    })

                except Exception as e:
                    log.debug(f"  Entry parse error: {e}")

            log.info(f"  {letter}: {len(seg_buyers)} total in {seg_label} so far")
            await asyncio.sleep(0.5)  # polite pause between letters

        log.info(f"Segment '{seg_label}': {len(seg_buyers)} unique buyers")
        all_buyers.extend(seg_buyers)

    # Final dedup pass (in case of overlap between segment scrapes)
    final, seen2 = [], set()
    for b in all_buyers:
        uid = b["id"] or b["profile_url"]
        if uid not in seen2:
            seen2.add(uid)
            final.append(b)

    log.info(f"Total unique buyers: {len(final)}")
    return final


# ── Send appointment request ──────────────────────────────────────────────────
async def send_request(page, buyer, message_text):
    """
    Navigate to buyer profile and send a meeting request.
    Uses the slot-aware URL from the diary → ricerca-slot-libero-ajax flow.
    """
    try:
        # Navigate to buyer profile/search result
        if buyer["profile_url"]:
            target = buyer["profile_url"]
        else:
            target = f"{BUYER_URL}/{buyer['id']}"

        await page.goto(target, wait_until="networkidle", timeout=15000)

        # Look for appointment request button
        # In ARE: usually "Richiedi appuntamento" or similar
        btn = await page.query_selector(
            "a.btn-appuntamento, "
            "button.btn-appuntamento, "
            "a[href*='ricerca-slot-libero'], "
            "a[href*='appuntamento'], "
            "button:has-text('appointment'), "
            "a:has-text('appointment'), "
            "a:has-text('Richiedi'), "
            "button:has-text('Richiedi')"
        )

        if not btn:
            return "no_appt_button"

        await btn.click()
        await page.wait_for_load_state("networkidle", timeout=10000)

        # Fill message textarea
        msg_area = await page.query_selector("textarea[name='msg'], textarea.form-control")
        if msg_area:
            await msg_area.fill(message_text)

        # Submit
        submit = await page.query_selector(
            "button[data-action='appuntamento'], "
            "button:has-text('Confirm'), "
            "button:has-text('Send'), "
            "input[type='submit'], "
            "button[type='submit']:not([data-action='rifiuta']):not([data-action='libera'])"
        )
        if submit:
            await submit.click()
            await page.wait_for_load_state("networkidle", timeout=10000)
            return "sent"
        else:
            return "no_submit_button"

    except PlaywrightTimeout:
        return "timeout"
    except Exception as e:
        log.error(f"Error for {buyer.get('company','?')}: {e}")
        return f"error"


# ── Wave runner ───────────────────────────────────────────────────────────────
async def run_wave(page, buyers, wave_number):
    """Cycle through message variants based on wave number, send to all buyers."""
    # Each buyer has a preferred variant; wave number shifts all variants by (wave-1)%4
    wave_shift = (wave_number - 1) % len(MESSAGES)
    log.info(f"=== WAVE {wave_number} | shift={wave_shift} | {len(buyers)} buyers ===")

    for i, buyer in enumerate(buyers):
        # Rotate variant: buyer's base variant + wave shift, mod total variants
        variant_idx  = (buyer["msg_variant"] + wave_shift) % len(MESSAGES)
        msg_template = MESSAGES[variant_idx]

        first_name   = (buyer["name"] or buyer["company"] or "there").split()[0]
        message_text = msg_template.format(name=first_name)

        status = await send_request(page, buyer, message_text)

        write_log(
            wave=wave_number,
            variant_idx=variant_idx,
            segment=buyer["segment"],
            buyer_id=buyer["id"],
            buyer_name=buyer["name"],
            buyer_company=buyer["company"],
            buyer_country=buyer["country"],
            status=status,
        )

        log.info(f"  [{i+1}/{len(buyers)}] {buyer['company']} ({buyer['segment']}) → {status}")
        await asyncio.sleep(1.5)

    log.info(f"=== Wave {wave_number} complete ===")


# ── Main ──────────────────────────────────────────────────────────────────────
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
