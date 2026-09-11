"""
TTG 2026 — Borracce di Poesia
Appointment Request Automation v3

Changes from v2:
- Targeted polling only (buyers marked YES in GitHub CSV)
- ~10-minute full cycle through targets
- Locked slot expiry monitoring with 30s retry in 5-min pre-expiry window
- Two-click booking confirmation
- Gmail notification on successful booking
"""

import asyncio
import sys
import csv
import logging
import os
import re
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Configuration ─────────────────────────────────────────────────────────────

BASE_URL  = "https://bme.iegexpo.it"

CET = timezone(timedelta(hours=1))
WINDOW_CLOSE = datetime(2026, 10, 8, 10, 0, 0, tzinfo=CET)

LOG_FILE = Path("ttg_bdp_log.csv")

SEGMENT_CATEGORIES = [
    ("Travel Agency",         "4416943",  2),  # → Variant 3 (general TO)
    ("Tour Operator",         "4416957",  2),  # → Variant 3 (general TO)
    ("Wholesaler",            "4416945",  2),  # → Variant 3
    ("Incentive House",       "4416956",  1),  # → Variant 2 (active/outdoor)
]

LETTERS = list("123ABCDEFGHIJKLMNOPQRSTUVWXYZ")
TARGETS_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQHA2pkFsJTrMLPz9yDqSng-fZ1-6Y_Inya668wtxVx4fQF6mBm_OW7AgIEzPMvuxGY7SDp5fFnAcaO/pub?output=csv"

# Polling interval between full cycles (seconds)
CYCLE_INTERVAL = 600  # 10 minutes

# Gmail config
GMAIL_USER      = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASS  = os.environ.get("GMAIL_APP_PASSWORD", "")
NOTIFY_EMAIL    = os.environ.get("NOTIFY_EMAIL", "operations@bestholidaysinitaly.com")

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

# ── Logging ───────────────────────────────────────────────────────────────────
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
                "timestamp", "cycle", "variant_index", "segment",
                "buyer_id", "buyer_name", "buyer_company", "buyer_country",
                "status", "notes"
            ])


def write_log(cycle, variant_idx, segment, buyer_id, buyer_name,
              buyer_company, buyer_country, status, notes=""):
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            datetime.now(CET).isoformat(),
            cycle, variant_idx, segment,
            buyer_id, buyer_name, buyer_company, buyer_country,
            status, notes
        ])


# ── Gmail notification ────────────────────────────────────────────────────────
def send_notification(subject, body):
    if not GMAIL_USER or not GMAIL_APP_PASS:
        log.warning("Gmail credentials not set — skipping notification")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = NOTIFY_EMAIL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_APP_PASS)
            smtp.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())
        log.info(f"Notification sent: {subject}")
    except Exception as e:
        log.error(f"Notification failed: {e}")


# ── Login ─────────────────────────────────────────────────────────────────────
async def dismiss_cookie_banner(page):
    try:
        btn = await page.query_selector("button#c-p-bn, button.c-bn[data-role='acceptAll'], a#accept-all, button:has-text('ACCEPT ALL COOKIES')")
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
    log.info("Logging in via autologin URL...")
    autologin_url = os.environ.get("BDP_AUTOLOGIN_URL", "")
    if not autologin_url:
        raise Exception("BDP_AUTOLOGIN_URL not set")
    await page.goto(autologin_url, wait_until="networkidle", timeout=30000)
    log.info(f"Post-autologin URL: {page.url}")
    if "login" in page.url and "autologin" not in page.url:
        raise Exception(f"Autologin failed: {page.url}")
    log.info("Login successful")


# ── Target CSV ────────────────────────────────────────────────────────────────
def fetch_targets(csv_url):
    import urllib.request, csv as csv_mod, io
    try:
        with urllib.request.urlopen(csv_url, timeout=10) as r:
            content = r.read().decode("utf-8")
        reader = csv_mod.DictReader(io.StringIO(content))
        rows = list(reader)
        if not rows:
            log.info("Target sheet empty — targeting all buyers")
            return None
        rows = [{k.strip(): v for k, v in row.items()} for row in rows]
        targets = {
            row["Buyer ID"].strip()
            for row in rows
            if row.get("Target", "").strip().upper() == "YES"
        }
        log.info(f"Target sheet: {len(targets)} buyers marked YES")
        return targets
    except Exception as e:
        log.warning(f"Could not fetch targets: {e} — targeting all buyers")
        return None


# ── Buyer scrape ──────────────────────────────────────────────────────────────
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
                    await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                except Exception as e:
                    log.warning(f"  Timeout {letter} p{page_num}: {e}")
                    break

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
                        uid_match = re.search(r"user=([0-9]+)", href or "")
                        if not uid_match:
                            continue
                        buyer_id = uid_match.group(1)
                        if buyer_id in seen_ids:
                            continue
                        seen_ids.add(buyer_id)
                        seg_count += 1

                        # Visit diary page to get full profile data
                        contact, country, website, address = "", "", "", ""
                        try:
                            diary_url = BASE_URL + "/ttg26/en/agenda-appuntamenti?user=" + buyer_id
                            await page.goto(diary_url, wait_until="domcontentloaded", timeout=6000)
                            header = await page.query_selector("header.row span")
                            if header:
                                raw = (await header.inner_text()).strip()
                                contact = raw.replace("Buyer attending:", "").strip()
                            addr_el = await page.query_selector("ul.user-details li div")
                            if addr_el:
                                addr_text = (await addr_el.inner_text()).strip()
                                lines = [l.strip() for l in addr_text.splitlines() if l.strip()]
                                address = " ".join(lines)
                                if lines:
                                    country = lines[-1]
                            web_el = await page.query_selector("ul.user-details + ul.user-details a[href^='http']")
                            if web_el:
                                website = (await web_el.get_attribute("href") or "").strip()
                        except Exception as e:
                            log.debug(f"Profile fetch error for {company}: {e}")

                        log.info(f"BUYER_DETAIL|{buyer_id}|{company}|{country}|{seg_label}|{contact}|||{website}|{address}")

                        all_buyers.append({
                            "id": buyer_id, "name": company, "company": company,
                            "country": country, "contact": contact,
                            "website": website, "address": address,
                            "appt_url": BASE_URL + "/ttg26/en/agenda-appuntamenti?user=" + buyer_id,
                            "segment": seg_label, "msg_variant": msg_idx,
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


# ── Parse locked slot expiry ──────────────────────────────────────────────────
def parse_expiry_seconds(expiry_str):
    """
    Parse expiry from scadenza_opzione_sua datetime string e.g. '2026-09-10 16:10'.
    Returns seconds until expiry from now (CET), or None if not found/past.
    """
    if not expiry_str:
        return None
    try:
        expiry_dt = datetime.strptime(expiry_str, "%Y-%m-%d %H:%M").replace(tzinfo=CET)
        secs = (expiry_dt - datetime.now(CET)).total_seconds()
        return int(secs) if secs > 0 else None
    except Exception:
        return None


def extract_locked_slots_from_page_source(html):
    """
    Extract locked slots from the calendar JSON embedded in page source.
    Returns list of dicts with slot time and expiry seconds.
    """
    import json as _json
    locked = []
    m = re.search(r"events:\s*(\[.*?\])", html, re.DOTALL)
    if not m:
        return locked
    try:
        events = _json.loads(m.group(1))
        for ev in events:
            if ev.get("stato") == "stato-opzionato" and ev.get("stato_mio") == "Free":
                expiry_str = ev.get("scadenza_opzione_sua")
                expiry_secs = parse_expiry_seconds(expiry_str)
                if expiry_secs and expiry_secs > 0:
                    locked.append({
                        "slot": ev.get("slot"),
                        "expiry_secs": expiry_secs,
                        "expiry_str": expiry_str,
                    })
    except Exception:
        pass
    return locked


# ── Scan and book buyer calendar ─────────────────────────────────────────────
async def scan_and_book(page, buyer, message_text):
    """
    Visit buyer calendar. Book free slot if found. Return locked slot info if relevant.
    """
    target = buyer.get("appt_url") or (BASE_URL + "/ttg26/en/agenda-appuntamenti?user=" + buyer["id"])
    try:
        await page.goto(target, wait_until="domcontentloaded", timeout=10000)
        try:
            await page.wait_for_selector(
                "div.fc-event.stato-libero, div.fc-event.stato-occupato, div.fc-event.stato-opzionato",
                timeout=10000
            )
            await asyncio.sleep(2)
        except PlaywrightTimeout:
            await page.goto(target, wait_until="domcontentloaded", timeout=10000)
            try:
                await page.wait_for_selector(
                    "div.fc-event.stato-libero, div.fc-event.stato-occupato, div.fc-event.stato-opzionato",
                    timeout=10000
                )
                await asyncio.sleep(2)
            except PlaywrightTimeout:
                return "no_calendar"

        free_slot = await page.query_selector("div.fc-event.stato-libero")
        if free_slot:
            return await _book_free_slot(page, free_slot, message_text)

        html = await page.content()
        locked_slots = extract_locked_slots_from_page_source(html)
        if locked_slots:
            best = min(locked_slots, key=lambda x: x["expiry_secs"])
            return ("locked", best["expiry_secs"], best.get("slot", ""))

        return "no_free_slot"

    except PlaywrightTimeout:
        return "timeout"
    except Exception as e:
        log.error(f"scan_and_book error for {buyer.get('company','?')}: {e}")
        return "error"


async def _book_free_slot(page, free_slot, message_text):
    """Click an already-located free slot element and complete the booking."""
    try:
        await free_slot.click()

        try:
            await page.wait_for_selector("div.modal-dialog", timeout=8000)
        except PlaywrightTimeout:
            await free_slot.click()
            try:
                await page.wait_for_selector("div.modal-dialog", timeout=5000)
            except PlaywrightTimeout:
                return "no_modal"

        msg_area = await page.query_selector("textarea[name='msg']")
        if msg_area:
            await msg_area.fill(message_text)

        submit = await page.query_selector(
            "button[data-action='/ttg26/en/richiedi-appuntamento-ajax']"
        )
        if not submit:
            return "no_submit"

        await dismiss_cookie_banner(page)
        await page.evaluate("document.getElementById('cc--main') && document.getElementById('cc--main').remove()")
        await submit.click()
        await asyncio.sleep(2)

        return "sent"

    except PlaywrightTimeout:
        return "timeout"
    except Exception as e:
        log.error(f"_book_free_slot error: {e}")
        return "error"


# ── Retry locked slot ─────────────────────────────────────────────────────────
async def monitor_locked_slot(page, buyer, expiry_secs, message_text, cycle):
    """
    Wait until ~5 minutes before expiry, then poll every 30s until slot frees.
    Books immediately when stato-libero appears.
    """
    PRE_EXPIRY_WINDOW = 300  # 5 minutes
    POLL_INTERVAL     = 30   # seconds

    wait_until_poll = expiry_secs - PRE_EXPIRY_WINDOW
    if wait_until_poll > 0:
        log.info(f"  [{buyer['company']}] Locked slot expires in {expiry_secs}s — "
                 f"will start polling in {wait_until_poll}s")
        await asyncio.sleep(wait_until_poll)

    log.info(f"  [{buyer['company']}] Entering 30s polling window (slot expires ~{PRE_EXPIRY_WINDOW}s)")

    deadline = datetime.now(CET) + timedelta(seconds=PRE_EXPIRY_WINDOW + 120)
    while datetime.now(CET) < deadline:
        result = await scan_and_book(page, buyer, message_text)
        if result == "sent":
            log.info(f"  [{buyer['company']}] BOOKED (was locked)")
            send_notification(
                subject=f"[TTG] Meeting booked — {buyer['company']}",
                body=(
                    f"A previously locked slot has freed up and been booked.\n\n"
                    f"Company: {buyer['company']}\n"
                    f"Country: {buyer['country']}\n"
                    f"Buyer ID: {buyer['id']}\n"
                    f"Time: {datetime.now(CET).strftime('%Y-%m-%d %H:%M CET')}"
                )
            )
            return "sent"
        elif result in ("no_calendar", "error"):
            break
        await asyncio.sleep(POLL_INTERVAL)

    log.info(f"  [{buyer['company']}] Locked slot did not free up in time")
    return "locked_expired"

# ── Main ──────────────────────────────────────────────────────────────────────
async def run_cycle(page, buyers, cycle_number, locked_queue):
    """
    Single pass through all target buyers.
    - Books free slots immediately
    - Queues locked slots for monitoring
    - Sends Gmail on successful booking
    """
    targets = fetch_targets(TARGETS_CSV_URL)
    if targets is not None:
        # Build buyer list directly from CSV — don't restrict to scraped buyers
        scraped_index = {b["id"]: b for b in buyers}
        filtered = []
        for buyer_id in targets:
            if buyer_id in scraped_index:
                filtered.append(scraped_index[buyer_id])
            else:
                filtered.append({
                    "id": buyer_id,
                    "company": buyer_id,
                    "name": "",
                    "segment": "Unknown",
                    "country": "",
                    "contact": "",
                    "msg_variant": hash(buyer_id) % len(MESSAGES),
                    "appt_url": BASE_URL + "/ttg26/en/agenda-appuntamenti?user=" + buyer_id,
                })
        log.info(f"After target filter: {len(filtered)} buyers this cycle")
    else:
        filtered = buyers
        log.info(f"No target filter — cycling all {len(filtered)} buyers")

    log.info(f"=== CYCLE {cycle_number} | {len(filtered)} buyers ===")
    no_cal_streak = 0

    for i, buyer in enumerate(filtered):
        if i > 0 and i % 50 == 0:
            log.info(f"Session refresh at buyer {i+1}...")
            try:
                await login(page)
                no_cal_streak = 0
            except Exception as e:
                log.error(f"Refresh failed: {e}")

        if no_cal_streak >= 5:
            log.info(f"Session likely expired (streak={no_cal_streak}), re-logging in...")
            try:
                await login(page)
                no_cal_streak = 0
            except Exception as e:
                log.error(f"Refresh failed: {e}")

        variant_idx  = buyer["msg_variant"] % len(MESSAGES)
        msg_template = MESSAGES[variant_idx]
        first_name   = (buyer["name"] or buyer["company"] or "there").split()[0]
        message_text = msg_template.format(name=first_name)

        result = await scan_and_book(page, buyer, message_text)

        if result == "sent":
            log.info(f"  [{i+1}/{len(filtered)}] {buyer['company']} \u2192 sent")
            write_log(cycle_number, variant_idx, buyer["segment"],
                      buyer["id"], buyer["name"], buyer["company"],
                      buyer["country"], "sent")
            send_notification(
                subject=f"[BDP TTG] Meeting booked \u2014 {buyer['company']}",
                body=(
                    f"A free slot has been booked.\n\n"
                    f"Company: {buyer['company']}\n"
                    f"Country: {buyer['country']}\n"
                    f"Segment: {buyer['segment']}\n"
                    f"Buyer ID: {buyer['id']}\n"
                    f"Account: Borracce di Poesia / Discovery Puglia (BDP)\n"
                    f"Time: {datetime.now(CET).strftime('%Y-%m-%d %H:%M CET')}"
                )
            )
            no_cal_streak = 0

        elif isinstance(result, tuple) and result[0] == "locked":
            expiry_secs = result[1]
            slot_text   = result[2] if len(result) > 2 else ""
            log.info(f"  [{i+1}/{len(filtered)}] {buyer['company']} \u2192 LOCKED (expires in {expiry_secs}s)")
            write_log(cycle_number, variant_idx, buyer["segment"],
                      buyer["id"], buyer["name"], buyer["company"],
                      buyer["country"], "locked", f"expires_in={expiry_secs}s")
            if expiry_secs < 86400:
                locked_queue.append({
                    "buyer": buyer,
                    "expiry_secs": expiry_secs,
                    "message_text": message_text,
                    "queued_at": datetime.now(CET),
                })
            no_cal_streak = 0

        elif result == "no_calendar":
            no_cal_streak += 1
            log.info(f"  [{i+1}/{len(filtered)}] {buyer['company']} \u2192 no_calendar")

        else:
            no_cal_streak = 0
            log.info(f"  [{i+1}/{len(filtered)}] {buyer['company']} \u2192 {result}")
        await asyncio.sleep(1.0)

    log.info(f"=== Cycle {cycle_number} complete ===")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    init_log()
    now = datetime.now(CET)
    log.info(f"BDP v3 started at {now.isoformat()}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page    = await context.new_page()

        await login(page)
        buyers = await scrape_buyers_by_segment(page)

        if not buyers:
            log.error("No buyers found.")
            await browser.close()
            return

        cycle = 1
        locked_queue = []  # list of dicts: {buyer, expiry_secs, message_text, queued_at}

        while datetime.now(CET) < WINDOW_CLOSE:
            cycle_start = datetime.now(CET)

            # Re-scrape every 10 cycles to catch new buyers
            if cycle > 1 and cycle % 10 == 0:
                log.info("Refreshing buyer list...")
                try:
                    buyers = await scrape_buyers_by_segment(page)
                except Exception as e:
                    log.error(f"Scrape refresh failed: {e}")

            try:
                await run_cycle(page, buyers, cycle, locked_queue)
            except Exception as e:
                log.error(f"Cycle {cycle} error: {e}")
                try:
                    await login(page)
                except Exception:
                    pass

            # Process locked queue — launch monitors for slots expiring soon
            now = datetime.now(CET)
            still_queued = []
            for item in locked_queue:
                elapsed = (now - item["queued_at"]).total_seconds()
                remaining = item["expiry_secs"] - elapsed
                if remaining <= 0:
                    log.info(f"Locked slot for {item['buyer']['company']} already expired — skipping")
                    continue
                if remaining < 86400:
                    log.info(f"Launching locked-slot monitor for {item['buyer']['company']} "
                             f"({remaining:.0f}s remaining)")
                    asyncio.create_task(
                        monitor_locked_slot(
                            page, item["buyer"], remaining,
                            item["message_text"], cycle
                        )
                    )
                else:
                    still_queued.append(item)
            locked_queue = still_queued

            cycle += 1
            elapsed_secs = (datetime.now(CET) - cycle_start).total_seconds()
            wait_secs = max(0, CYCLE_INTERVAL - elapsed_secs)
            if wait_secs > 0:
                log.info(f"Cycle done in {elapsed_secs:.0f}s — next cycle in {wait_secs:.0f}s")
                await asyncio.sleep(wait_secs)

        log.info("Window closed. BDP v3 complete.")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
