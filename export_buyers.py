"""
TTG 2026 — Buyer Directory Export
Scrapes all buyers and exports to Excel with full details.
Run once manually: python export_buyers.py
Requires BHI_AUTOLOGIN_URL environment variable.
"""

import asyncio
import os
import re
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from datetime import datetime

BASE_URL  = "https://bme.iegexpo.it"
BUYER_URL = f"{BASE_URL}/ttg26/en/ricerca-buyer"

SEGMENT_CATEGORIES = [
    ("Tour Operator",         "4416957"),
    ("Luxury Travel Advisor", "26872093"),
    ("Incentive House",       "4416956"),
    ("Travel Agency",         "4416943"),
    ("Wholesaler",            "4416945"),
    ("OLTA/OTA",              "4416947"),
    ("PCO",                   "4416955"),
]

LETTERS = list("123ABCDEFGHIJKLMNOPQRSTUVWXYZ")

PRIORITY_COUNTRIES = {
    "France", "United Kingdom", "United States", "Canada",
    "Australia", "Germany", "Spain", "Brazil"
}


async def login(page):
    autologin_url = os.environ.get("BHI_AUTOLOGIN_URL", "")
    if not autologin_url:
        raise Exception("BHI_AUTOLOGIN_URL not set")
    await page.goto(autologin_url, wait_until="networkidle", timeout=30000)
    print(f"Logged in: {page.url}")


async def get_buyer_profile(page, profile_url):
    """Fetch additional details from buyer profile page."""
    try:
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(1000)

        # Extract profile details
        details = {}

        # Contact name
        name_el = await page.query_selector(".contact-name, .referente, h2.nome")
        if name_el:
            details["contact"] = (await name_el.inner_text()).strip()

        # Website
        web_el = await page.query_selector("a[href*='http']:not([href*='bme.iegexpo'])")
        if web_el:
            details["website"] = await web_el.get_attribute("href")

        # Description/bio
        desc_el = await page.query_selector(".descrizione, .description, .bio")
        if desc_el:
            details["description"] = (await desc_el.inner_text()).strip()[:200]

        return details
    except Exception:
        return {}


async def scrape_all_buyers(page):
    all_buyers = []
    seen_ids   = set()

    for seg_label, categoria_val in SEGMENT_CATEGORIES:
        print(f"\nScraping: {seg_label}")
        count = 0

        for letter in LETTERS:
            page_num = 1
            while True:
                url = (f"{BUYER_URL}?ragione_sociale_iniziale={letter}"
                       f"&categoria={categoria_val}&submit=1&page={page_num}")
                try:
                    await page.goto(url, wait_until="networkidle", timeout=20000)
                except Exception:
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
                        m = re.search(r"user=(\d+)", href or "")
                        buyer_id = m.group(1) if m else ""
                        if not buyer_id or buyer_id in seen_ids:
                            continue

                        country_el = await entry.query_selector("p.risultati-info span")
                        country = (await country_el.inner_text()).strip() if country_el else ""

                        # Availability
                        sr_inner = await entry.query_selector(".sr-inner")
                        avail = ""
                        if sr_inner:
                            cls = await sr_inner.get_attribute("class") or ""
                            if "semaforo-1" in cls:
                                avail = "Free slots"
                            elif "semaforo-2" in cls:
                                avail = "Partially booked"
                            elif "semaforo-3" in cls:
                                avail = "Full"

                        seen_ids.add(buyer_id)
                        count += 1
                        all_buyers.append({
                            "id":          buyer_id,
                            "company":     company,
                            "country":     country,
                            "segment":     seg_label,
                            "availability": avail,
                            "priority":    "YES" if country in PRIORITY_COUNTRIES else "",
                            "diary_url":   f"{BASE_URL}{href}",
                        })
                    except Exception:
                        pass

                next_link = await page.query_selector("ul.pagination li.last a")
                if next_link:
                    next_href = await next_link.get_attribute("href") or ""
                    if f"page={page_num}" in next_href:
                        break
                    page_num += 1
                else:
                    break
                await asyncio.sleep(0.3)

            await asyncio.sleep(0.3)

        print(f"  {seg_label}: {count} buyers")

    # Deduplicate
    final, seen2 = [], set()
    for b in all_buyers:
        if b["id"] not in seen2:
            seen2.add(b["id"])
            final.append(b)

    print(f"\nTotal unique buyers: {len(final)}")
    return final


def export_to_excel(buyers, filename="TTG_2026_Buyers.xlsx"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "All Buyers"

    # Header style
    header_font   = Font(bold=True, color="FFFFFF")
    header_fill   = PatternFill("solid", fgColor="1F334D")
    priority_fill = PatternFill("solid", fgColor="E8F5E9")
    free_fill     = PatternFill("solid", fgColor="F3E5F5")

    headers = [
        "Company", "Country", "Segment", "Availability",
        "Priority Market", "Buyer ID", "Diary URL"
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font      = header_font
        cell.fill      = header_fill
        cell.alignment = Alignment(horizontal="center")

    # Data
    for b in sorted(buyers, key=lambda x: (x["priority"] != "YES", x["country"], x["company"])):
        row = [
            b["company"], b["country"], b["segment"],
            b["availability"], b["priority"],
            b["id"], b["diary_url"]
        ]
        ws.append(row)
        last_row = ws.max_row
        if b["priority"] == "YES":
            for cell in ws[last_row]:
                cell.fill = priority_fill
        if b["availability"] == "Free slots":
            ws[last_row][3].fill = free_fill

    # Column widths
    widths = [40, 20, 22, 18, 14, 12, 60]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[chr(64+i)].width = w

    # Summary sheet
    ws2 = wb.create_sheet("Summary")
    ws2.append(["TTG 2026 Buyer Export", datetime.now().strftime("%d %b %Y %H:%M")])
    ws2.append([])
    ws2.append(["Total unique buyers", len(buyers)])
    ws2.append(["Priority markets", sum(1 for b in buyers if b["priority"] == "YES")])
    ws2.append(["Free slots available", sum(1 for b in buyers if b["availability"] == "Free slots")])
    ws2.append([])
    ws2.append(["By segment:"])
    from collections import Counter
    for seg, cnt in Counter(b["segment"] for b in buyers).most_common():
        ws2.append([seg, cnt])
    ws2.append([])
    ws2.append(["By country (top 20):"])
    for country, cnt in Counter(b["country"] for b in buyers).most_common(20):
        ws2.append([country, cnt])

    wb.save(filename)
    print(f"\nExported to: {filename}")
    return filename


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page    = await (await browser.new_context()).new_page()
        await login(page)
        buyers  = await scrape_all_buyers(page)
        export_to_excel(buyers)
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
