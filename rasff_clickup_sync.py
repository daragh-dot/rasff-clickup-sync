"""
RASFF Window -> ClickUp sync.

Fetches recent notifications from RASFF Window's real search/export API
(discovered via browser DevTools - see below), filters them down to what's
actually relevant to your business using keyword rules, and creates a
ClickUp task for each new match.

Dedup strategy: before creating a task, we check existing tasks in the target
ClickUp list for the RASFF reference number (stored in the task name). This
means the script is stateless and safe to re-run / run in CI without needing
a persisted database.

-----------------------------------------------------------------------------
SETUP
-----------------------------------------------------------------------------
1. pip install -r requirements.txt
2. Set environment variables (or edit CONFIG below directly):
     CLICKUP_API_TOKEN   - your personal ClickUp API token
     CLICKUP_LIST_ID     - the ClickUp list to create tasks in
3. Edit KEYWORDS below to match your actual exposure (ingredients, hazards,
   countries). Keep this list tight - it's your main defense against noise.
4. Run: python rasff_clickup_sync.py

-----------------------------------------------------------------------------
ABOUT THE RASFF ENDPOINT
-----------------------------------------------------------------------------
This calls RASFF Window's own backend search endpoint directly (the same one
the website's search/export buttons use), found via browser DevTools:

  POST https://webgate.ec.europa.eu/rasff-window/backend/public/notification/search/export/en/

It's not formally documented as a public API, so if the EU changes it, this
will start failing loudly (non-200 response / unexpected JSON shape) rather
than silently - the error handling below is written to make that obvious.
-----------------------------------------------------------------------------
"""

import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import requests

# =============================================================================
# CONFIG - this is the part you should tune for your business
# =============================================================================

CLICKUP_API_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")
CLICKUP_LIST_ID = os.environ.get("CLICKUP_LIST_ID", "")

# How many days back to search each run. Keep this a bit wider than your
# schedule interval (e.g. 3 days for a daily run) so a missed run doesn't
# lose notifications.
LOOKBACK_DAYS = 3

RASFF_SEARCH_PAGE_URL = "https://webgate.ec.europa.eu/rasff-window/screen/search"
RASFF_EXPORT_API_URL = (
    "https://webgate.ec.europa.eu/rasff-window/backend/public/notification/search/export/en/"
)
RASFF_ITEMS_PER_PAGE = 500  # comfortably above expected daily/weekly volume

# Keywords are matched case-insensitively against the notification's subject,
# product category, product type, and hazard text. A notification is a match
# if ANY keyword matches. Keep this narrow and specific, or you'll get
# flooded.
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


def _get(d: dict, *path, default=""):
    """Safely walk a nested dict, returning `default` if any key is missing."""
    cur = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur if cur is not None else default


@dataclass
class Notification:
    reference: str
    date: str
    subject: str
    product_category: str = ""
    product_type: str = ""
    hazard_text: str = ""
    notifying_country: str = ""
    classification: str = ""  # e.g. "Alert", "Information", "Border Rejection"
    notif_id: str = ""

    @property
    def url(self) -> str:
        if self.notif_id:
            return f"https://webgate.ec.europa.eu/rasff-window/screen/notification/{self.notif_id}"
        return RASFF_SEARCH_PAGE_URL

    @classmethod
    def from_api_record(cls, rec: dict) -> "Notification":
        hazards = rec.get("hazards") or []
        if isinstance(hazards, list):
            hazard_text = "; ".join(str(h) for h in hazards)
        else:
            hazard_text = str(hazards)

        return cls(
            reference=str(_get(rec, "reference")),
            date=str(_get(rec, "ecValidationDate")),
            subject=str(_get(rec, "subject")),
            product_category=str(_get(rec, "productCategory", "description")),
            product_type=str(_get(rec, "productType", "description")),
            hazard_text=hazard_text,
            notifying_country=str(_get(rec, "notifyingCountry", "organizationName")),
            classification=str(_get(rec, "notificationClassification", "description")),
            notif_id=str(_get(rec, "notifId")),
        )

    def matches_keywords(self, keywords: list[str]) -> list[str]:
        haystack = " ".join(
            [self.subject, self.product_category, self.product_type, self.hazard_text]
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
            f"**Product type:** {self.product_type}\n"
            f"**Hazard:** {self.hazard_text}\n"
            f"**Notifying country:** {self.notifying_country}\n\n"
            f"**Link:** {self.url}\n"
        )


# =============================================================================
# STEP 1: FETCH NOTIFICATIONS FROM RASFF WINDOW'S SEARCH API
# =============================================================================


def fetch_notifications(lookback_days: int) -> list[Notification]:
    """
    Calls RASFF Window's own search/export backend endpoint directly (found
    via browser DevTools network inspection - see module docstring).
    """
    date_from = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%d-%m-%Y 00:00:00")
    date_to = datetime.utcnow().strftime("%d-%m-%Y 23:59:59")

    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (compatible; EarthChimpRasffSync/1.0; "
                "+https://github.com/) requests-python"
            ),
            "X-Requested-With": "XMLHttpRequest",
        }
    )

    # Visit the search page first so the server can set session cookies -
    # mirrors what a real browser does before calling the export endpoint.
    try:
        session.get(RASFF_SEARCH_PAGE_URL, timeout=30)
    except requests.RequestException as e:
        print(f"WARNING: could not pre-load search page for cookies ({e}). Continuing anyway.")

    payload = {
        "parameters": {"pageNumber": 1, "itemsPerPage": RASFF_ITEMS_PER_PAGE},
        "notificationReference": None,
        "subject": None,
        "ecValidDateFrom": date_from,
        "ecValidDateTo": date_to,
        "notifyingCountry": None,
        "originCountry": None,
        "distributionCountry": None,
        "notificationType": None,
        "notificationStatus": None,
        "notificationClassification": None,
        "notificationBasis": None,
        "productCategory": None,
        "actionTaken": None,
        "hazardCategory": None,
        "riskDecision": None,
    }

    resp = session.post(RASFF_EXPORT_API_URL, json=payload, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(
            f"RASFF API returned HTTP {resp.status_code}. "
            f"The endpoint may have changed - check response body:\n{resp.text[:1000]}"
        )

    try:
        data = resp.json()
    except ValueError as e:
        raise RuntimeError(
            f"RASFF API did not return valid JSON (endpoint may have changed): {e}\n"
            f"Response body starts with: {resp.text[:500]}"
        )

    # The response shape wasn't fully confirmed during development - handle
    # a plain list or a few likely wrapper-key variants, and fail loudly with
    # a clear message if none match, rather than silently returning nothing.
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        records = None
        for key in ("content", "items", "results", "data", "notifications"):
            if key in data and isinstance(data[key], list):
                records = data[key]
                break
        if records is None:
            raise RuntimeError(
                f"RASFF API returned an unexpected JSON shape (dict with keys: "
                f"{list(data.keys())}). Update fetch_notifications() to match. "
                f"First 500 chars: {str(data)[:500]}"
            )
    else:
        raise RuntimeError(f"RASFF API returned unexpected JSON type: {type(data)}")

    return [Notification.from_api_record(rec) for rec in records]


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
    tag = re.sub(r"[^a-z0-9]+", "-", notif.hazard_text.lower()).strip("-")[:50]
    payload = {
        "name": notif.clickup_task_name(),
        "description": notif.clickup_description(),
        "tags": [tag] if tag else [],
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
    notifications = fetch_notifications(LOOKBACK_DAYS)
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