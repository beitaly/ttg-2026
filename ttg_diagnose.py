"""
TTG Diagnostic Script
Visits one buyer, waits for full calendar render, logs all fc-event elements.
"""
import asyncio, os, sys, logging
from datetime import datetime, timezone, timedelta
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
    format="%(asctime)s [INFO] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)

BASE_URL     = "https://bme.iegexpo.it"
AUTOLOGIN    = os.environ.get("BHI_AUTOLOGIN_URL", "")
BUYER_URL    = BASE_URL + "/ttg26/en/agenda-appuntamenti?user=32287849"

async def main():
    if not AUTOLOGIN:
        log.error("BHI_AUTOLOGIN_URL not set"); return

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page    = await browser.new_page()

        # Login
        log.info("Logging in...")
        await page.goto(AUTOLOGIN, wait_until="domcontentloaded", timeout=15000)
        log.info(f"Post-login URL: {page.url}")

        # Dismiss cookie banner
        try:
            btn = await page.query_selector("button#c-p-bn, button:has-text('ACCEPT ALL COOKIES')")
            if btn:
                await btn.click(timeout=2000)
                await asyncio.sleep(0.5)
        except Exception: pass
        try:
            await page.evaluate("document.getElementById('cc--main') && document.getElementById('cc--main').remove()")
        except Exception: pass

        # Visit buyer
        log.info(f"Visiting: {BUYER_URL}")
        await page.goto(BUYER_URL, wait_until="domcontentloaded", timeout=15000)
        log.info("Page loaded — waiting 4s for FullCalendar gotoDate to fire...")
        await asyncio.sleep(4)

        # Take screenshot
        await page.screenshot(path="/mnt/user-data/outputs/ttg_diagnose.png", full_page=True)
        log.info("Screenshot saved: ttg_diagnose.png")

        # Log all fc-event elements
        events = await page.query_selector_all("div.fc-event")
        log.info(f"Total div.fc-event elements found: {len(events)}")
        for i, ev in enumerate(events):
            cls  = await ev.get_attribute("class")
            text = (await ev.inner_text()).strip().replace("\n", " ")[:80]
            log.info(f"  [{i+1}] class='{cls}' text='{text}'")

        # Specifically check for stato-libero
        free = await page.query_selector_all("div.fc-event.stato-libero")
        log.info(f"Free slots (stato-libero): {len(free)}")
        for i, el in enumerate(free):
            text = (await el.inner_text()).strip().replace("\n", " ")
            log.info(f"  FREE [{i+1}]: {text}")

        # Log calendar header to confirm we're on the right week
        header = await page.query_selector(".fc-header .fc-button-today, .fc-head")
        col_headers = await page.query_selector_all(".fc-day-header")
        for h in col_headers:
            log.info(f"  Calendar column: {(await h.inner_text()).strip()}")

        # Log full calendar HTML for inspection
        cal = await page.query_selector("#calendar")
        if cal:
            html = await cal.inner_html()
            log.info(f"Calendar HTML length: {len(html)} chars")
            # Log first 3000 chars
            log.info("Calendar HTML (first 3000 chars):")
            log.info(html[:3000])

        await browser.close()
        log.info("Done.")

asyncio.run(main())
