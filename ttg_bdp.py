"""
TTG 2026 — Borracce di Poesia
Appointment Request Automation Script v2
Same structure as BHI v2 but with BDP credentials and messages.
"""

import asyncio
import csv
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

BASE_URL  = "https://www.ttgexpo.it"
LOGIN_URL = f"{BASE_URL}/ttg26/en/login"
BUYER_URL = f"{BASE_URL}/ttg26/en/ricerca-buyer"

# Credentials loaded from Railway environment variables (set in Railway dashboard)
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
        logging.StreamHandler(),
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
    log.info("Logging in...")
    await page.goto(LOGIN_URL, wait_until="networkidle")
    try:
        await page.click("button:has-text('ACCEPT ALL')", timeout=3000)
    except PlaywrightTimeout:
        pass
    await page.fill("input[name='userid']", CREDENTIALS["email"])
    await page.fill("input[name='password']", CREDENTIALS["password"])
    await page.click("button[type='submit'], input[type='submit']")
    await page.wait_for_url("**/default**", timeout=15000)
    log.info("Login successful.")


async def scrape_buyers_by_segment(page):
    all_buyers = []
    seen_ids   = set()

    for seg_label, categoria_val, msg_idx in SEGMENT_CATEGORIES:
        log.info(f"Scraping: {seg_label}")
        seg_buyers = []

        for letter in LETTERS:
            url = (f"{BUYER_URL}?ragione_sociale_iniziale={letter}"
                   f"&categoria={categoria_val}&submit=1")
            try:
                await page.goto(url, wait_until="networkidle", timeout=20000)
            except PlaywrightTimeout:
                continue

            entries = await page.query_selector_all(
                "table.risultati tbody tr, "
                ".risultati .risultato, "
                ".search-results tr[data-id], "
                "tr.buyer-row, .scheda-breve"
            )
            if not entries:
                entries = await page.query_selector_all("table tbody tr:not(:first-child)")

            for entry in entries:
                try:
                    link = await entry.query_selector(
                        "a[href*='/ttg26/en/ricerca-buyer/'], a[href*='/scheda/'], "
                        "a.btn-appuntamento, a[href*='slot'], a[href]"
                    )
                    href = await link.get_attribute("href") if link else ""
                    profile_url = BASE_URL + href if href and href.startswith("/") else href or ""

                    buyer_id = ""
                    m = re.search(r"/(\d+)/?(?:\?|$)", href or "")
                    if m:
                        buyer_id = m.group(1)

                    uid = buyer_id or profile_url
                    if not uid or uid in seen_ids:
                        continue

                    cells = await entry.query_selector_all("td")
                    texts = [(await c.inner_text()).strip() for c in cells if (await c.inner_text()).strip()]

                    company = texts[0] if texts else ""
                    country = texts[1] if len(texts) > 1 else ""
                    name    = texts[2] if len(texts) > 2 else company

                    seen_ids.add(uid)
                    seg_buyers.append({
                        "id": buyer_id, "name": name, "company": company,
                        "country": country, "profile_url": profile_url,
                        "segment": seg_label, "msg_variant": msg_idx,
                    })
                except Exception:
                    pass

            await asyncio.sleep(0.5)

        log.info(f"  '{seg_label}': {len(seg_buyers)} buyers")
        all_buyers.extend(seg_buyers)

    final, seen2 = [], set()
    for b in all_buyers:
        uid = b["id"] or b["profile_url"]
        if uid not in seen2:
            seen2.add(uid)
            final.append(b)

    log.info(f"Total unique buyers: {len(final)}")
    return final


async def send_request(page, buyer, message_text):
    try:
        target = buyer["profile_url"] or f"{BUYER_URL}/{buyer['id']}"
        await page.goto(target, wait_until="networkidle", timeout=15000)

        btn = await page.query_selector(
            "a.btn-appuntamento, button.btn-appuntamento, "
            "a[href*='ricerca-slot-libero'], a[href*='appuntamento'], "
            "button:has-text('appointment'), a:has-text('appointment'), "
            "a:has-text('Richiedi'), button:has-text('Richiedi')"
        )
        if not btn:
            return "no_appt_button"

        await btn.click()
        await page.wait_for_load_state("networkidle", timeout=10000)

        msg_area = await page.query_selector("textarea[name='msg'], textarea.form-control")
        if msg_area:
            await msg_area.fill(message_text)

        submit = await page.query_selector(
            "button[data-action='appuntamento'], button:has-text('Confirm'), "
            "button:has-text('Send'), input[type='submit'], "
            "button[type='submit']:not([data-action='rifiuta']):not([data-action='libera'])"
        )
        if submit:
            await submit.click()
            await page.wait_for_load_state("networkidle", timeout=10000)
            return "sent"
        return "no_submit_button"

    except PlaywrightTimeout:
        return "timeout"
    except Exception as e:
        log.error(f"Error: {e}")
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
