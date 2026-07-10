"""
RASFF Window -> ClickUp sync.

Scrapes recent notifications from RASFF Window (public EU food/feed safety
alert database), filters them down to what's actually relevant to your
business using keyword rules, and creates a ClickUp task for each new match.

Dedup strategy: before creating a task, we check existing tasks in the target
ClickUp list for the RASFF reference number (stored in the task name). This
means the script is stateless and safe to re-run / run in CI without needing
a persisted database.

-----------------------------------------------------------------------------
SETUP
-----------------------------------------------------------------------------
1. pip install -r requirements.txt
2. playwright install chromium
3. Set environment variables (or edit CONFIG below directly):
     CLICKUP_API_TOKEN   - your personal ClickUp API token
     CLICKUP_LIST_ID     - the ClickUp list to create tasks in
4. Edit KEYWORDS below to match your actual exposure (ingredients, hazards,
   countries). Keep this list tight - it's your main defense against noise.
5. Run: python rasff_clickup_sync.py

-----------------------------------------------------------------------------
IF YOU LATER GET THE REAL RASFF API ENDPOINT
-----------------------------------------------------------------------------
Grab the request via browser DevTools (Network tab -> XHR/Fetch -> copy as
cURL) while performing a search on https://webgate.ec.europa.eu/rasff-window/screen/search
and swap out `fetch_notifications_via_browser()` for a plain `requests.get(...)`
call. That'll be faster and won't need a browser at all.
-----------------------------------------------------------------------------
"""

import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import requests
from playwright.sync_api import sync_playwright

# =============================================================================
# CONFIG - this is the part you should tune for your business
# =============================================================================

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_LIST_ID = os.environ.get("CLICKUP_LIST_ID", "")

# How many days back to search each run. Keep this a bit wider than your
# schedule interval (e.g. 3 days for a daily run) so a missed run doesn't
# lose notifications.
LOOKBACK_DAYS = 3

RASFF_SEARCH_URL = "https://webgate.ec.europa.eu/rasff-window/screen/search"

# Keywords are matched case-insensitively against the notification's subject,
# product category, and hazard category text. A notification is a match if
# ANY keyword group below matches. Keep groups narrow and specific rather
# than broad, or you'll get flooded.
#
# Tuned to EarthChimp's actual exposure: heavy metals in protein/cocoa,
# your specific ingredient suppliers/origins, and allergens you declare.
KEYWORDS = [
    # Hazard-led (highest signal for your current lead/heavy-metals work)
    "lead", "cadmium", "arsenic", "mercury", "heavy metal",
    # Ingredient-led
    "pea protein", "protein isolate", "cocoa", "chocolate", "tapioca",
    "carob", "psyllium", "chia", "acacia gum",
    # Product-category-led
    "protein powder", "food supplement", "dietetic food", "plant-based",
    # Allergens you declare on-pack (only relevant if undeclared/mislabelled
    # notifications - still worth knowing about for competitor/industry signal)
    "undeclared soy", "undeclared milk", "undeclared nut", "undeclared sesame",
    # Origin countries relevant to your supply chain
    "china", "yantai",
]

# =============================================================================
# DATA MODEL
# =============================================================================


@dataclass
class Notification:
    reference: str
    date: str
    subject: str
    product_category: str = ""
    hazard_category: str = ""
    notifying_country: str = ""
    classification: str = ""  # e.g. "Alert", "Information", "Border Rejection"
    url: str = ""

    def matches_keywords(self, keywords: list[str]) -> list[str]:
        haystack = " ".join(
            [self.subject, self.product_category, self.hazard_category]
        ).lower()
        return [kw for kw in keywords if kw.lower() in haystack]

    def clickup_task_name(self) -> str:
        return f"[{self.reference}] {self.subject}"[:250]

    def clickup_description(self) -> str:
        return (
            f"**RASFF Reference:** {self.reference}\n"
            f"**Date:** {self.date}\n"
            f"**Classification:** {self.classification}\n"
            f"**Product category:** {self.product_category}\n"
            f"**Hazard category:** {self.hazard_category}\n"
            f"**Notifying country:** {self.notifying_country}\n\n"
            f"**Link:** {self.url}\n"
        )


# =============================================================================
# STEP 1: FETCH NOTIFICATIONS FROM RASFF WINDOW
# =============================================================================


def fetch_notifications_via_browser(lookback_days: int) -> list[Notification]:
    """
    Loads the RASFF Window search UI in a headless browser, runs a search for
    the lookback window, and scrapes the results table.

    NOTE: This relies on the current DOM structure of RASFF Window
    (webgate.ec.europa.eu/rasff-window/screen/search). If the EU updates the
    site layout, the CSS selectors below will need adjusting - run with
    headless=False locally to see what changed.
    """
    date_from = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    date_to = datetime.utcnow().strftime("%Y-%m-%d")

    notifications: list[Notification] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(RASFF_SEARCH_URL, wait_until="networkidle", timeout=60000)

        # --- Fill in the date range and trigger search ---
        # These selectors are best-effort placeholders based on the public
        # RASFF Window layout. Adjust after inspecting the live page
        # (right-click a field -> Inspect) if they don't match.
        try:
            page.fill('input[name="dateFrom"]', date_from)
            page.fill('input[name="dateTo"]', date_to)
            page.click('button:has-text("Search")')
            page.wait_for_selector("table tbody tr", timeout=30000)
        except Exception as e:
            print(f"WARNING: could not drive search form as expected ({e}). "
                  f"Falling back to scraping whatever is currently loaded.")

        rows = page.query_selector_all("table tbody tr")
        for row in rows:
            cells = [c.inner_text().strip() for c in row.query_selector_all("td")]
            if len(cells) < 4:
                continue
            link_el = row.query_selector("a")
            href = link_el.get_attribute("href") if link_el else ""
            full_url = href if href.startswith("http") else f"https://webgate.ec.europa.eu{href}"

            # Column order varies - this is a best-effort mapping. Print
            # `cells` once locally to confirm/adjust the indices for your view.
            notif = Notification(
                reference=cells[0] if len(cells) > 0 else "",
                date=cells[1] if len(cells) > 1 else "",
                classification=cells[2] if len(cells) > 2 else "",
                subject=cells[3] if len(cells) > 3 else "",
                product_category=cells[4] if len(cells) > 4 else "",
                hazard_category=cells[5] if len(cells) > 5 else "",
                notifying_country=cells[6] if len(cells) > 6 else "",
                url=full_url,
            )
            if notif.reference:
                notifications.append(notif)

        browser.close()

    return notifications


# =============================================================================
# STEP 2: CLICKUP INTEGRATION
# =============================================================================

CLICKUP_API_BASE = "https://api.clickup.com/api/v2"


def clickup_headers() -> dict:
    return {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}


def get_existing_references(list_id: str) -> set[str]:
    """
    Pulls task names from the target list and extracts any RASFF reference
    numbers already present (format: [YYYY.NNNN] at the start of the name),
    so we don't create duplicate tasks.
    """
    refs: set[str] = set()
    page = 0
    while True:
        resp = requests.get(
            f"{CLICKUP_API_BASE}/list/{list_id}/task",
            headers=clickup_headers(),
            params={"page": page, "include_closed": "true"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        tasks = data.get("tasks", [])
        if not tasks:
            break
        for t in tasks:
            m = re.match(r"^\[([^\]]+)\]", t.get("name", ""))
            if m:
                refs.add(m.group(1))
        if data.get("last_page", True):
            break
        page += 1
    return refs


def create_clickup_task(list_id: str, notif: Notification) -> None:
    priority = 1 if notif.classification.lower().startswith("alert") else 3  # 1=Urgent, 3=Normal
    payload = {
        "name": notif.clickup_task_name(),
        "description": notif.clickup_description(),
        "tags": [t for t in [notif.hazard_category.lower().replace(" ", "-")] if t],
        "priority": priority,
    }
    resp = requests.post(
        f"{CLICKUP_API_BASE}/list/{list_id}/task",
        headers=clickup_headers(),
        json=payload,
        timeout=30,
    )
    if resp.status_code >= 300:
        print(f"ERROR creating task for {notif.reference}: {resp.status_code} {resp.text}")
    else:
        print(f"Created ClickUp task for {notif.reference}: {notif.subject[:60]}")


# =============================================================================
# MAIN
# =============================================================================


def main() -> int:
    if not CLICKUP_API_TOKEN or not CLICKUP_LIST_ID:
        print("ERROR: set CLICKUP_API_TOKEN and CLICKUP_LIST_ID environment variables.")
        return 1

    print(f"Fetching RASFF notifications from the last {LOOKBACK_DAYS} day(s)...")
    notifications = fetch_notifications_via_browser(LOOKBACK_DAYS)
    print(f"Found {len(notifications)} notifications in the search window.")

    matches = []
    for n in notifications:
        hit_keywords = n.matches_keywords(KEYWORDS)
        if hit_keywords:
            matches.append((n, hit_keywords))

    print(f"{len(matches)} matched your keyword filters.")
    if not matches:
        return 0

    existing_refs = get_existing_references(CLICKUP_LIST_ID)
    print(f"{len(existing_refs)} notifications already tracked in ClickUp.")

    created = 0
    for notif, hit_keywords in matches:
        if notif.reference in existing_refs:
            continue
        print(f"  -> {notif.reference}: matched on {hit_keywords}")
        create_clickup_task(CLICKUP_LIST_ID, notif)
        created += 1
        time.sleep(0.5)  # be polite to ClickUp's rate limits

    print(f"Done. Created {created} new task(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
