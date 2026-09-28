"""
Mirror application status changes into the Google Sheets job tracker.

Optional: does nothing unless both env vars are set
  GOOGLE_SERVICE_ACCOUNT_JSON  path to a service-account key file
  JOB_TRACKER_SHEET_ID         the spreadsheet ID (from its URL)
and the sheet is shared with the service account's email as an Editor.

Sheet layout (every tab): A=Company, C=Role, D=Contact, F=Link, G=Status, H=Notes.
A company already listed on some tab gets its row filled in; otherwise a row is appended
to the first tab. Contact (D) and Resume Used (I) are never touched.
"""

import logging
import os
import re

SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SHEET_ID = os.getenv("JOB_TRACKER_SHEET_ID")

STATUS_LABELS = {
    "applied": "Applied",
    "phone_screen": "Phone Screen",
    "interview": "Interviewing",
    "offer": "Offer",
    "rejected": "Rejected",
    "withdrawn": "Withdrawn",
}

_SUFFIXES = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|technologies|systems|labs)\b\.?|[.,]", re.I)


def normalize_company(name):
    return " ".join(_SUFFIXES.sub(" ", str(name).lower()).split())


def enabled():
    return bool(SERVICE_ACCOUNT_JSON and SHEET_ID)


def _open_sheet():
    import gspread  # imported lazily so the bot runs without it when sync is off
    return gspread.service_account(filename=SERVICE_ACCOUNT_JSON).open_by_key(SHEET_ID)


def find_row(worksheets, company, title, url=""):
    """Return (worksheet, row_number, is_status_update) for where this application belongs, or (None, None, False).

    Match order: same posting link → same company+role → same company with an empty Role cell.
    """
    target = normalize_company(company)
    blank_match = None
    for ws in worksheets:
        rows = ws.get_all_values()
        for i, row in enumerate(rows[2:], start=3):  # row 1 = header, row 2 = placeholder
            row = row + [""] * (8 - len(row))
            if url and row[5].strip() == url:
                return ws, i, True
            if normalize_company(row[0]) != target or not target:
                continue
            if row[2].strip().lower() == str(title).strip().lower():
                return ws, i, True
            if not row[2].strip() and blank_match is None:
                blank_match = (ws, i, False)
    return blank_match or (None, None, False)


def record(company, title, url, status, notes="", sheet=None):
    """Create or update the tracker row. Returns a short description of what changed, or None."""
    if sheet is None:
        if not enabled():
            return None
        sheet = _open_sheet()
    label = STATUS_LABELS.get(status, status.replace("_", " ").title())
    worksheets = sheet.worksheets()
    ws, row, is_update = find_row(worksheets, company, title, url)

    if ws is not None and is_update:
        ws.update_acell(f"G{row}", label)
        return f"{ws.title} row {row}: status → {label}"
    if ws is not None:
        ws.update(f"C{row}", [[title]])
        ws.update(f"F{row}:H{row}", [[url, label, notes]])
        return f"{ws.title} row {row}: filled {title} ({label})"

    ws = worksheets[0]
    ws.append_row([company, "", title, "", "", url, label, notes],
                  value_input_option="USER_ENTERED", table_range="A1")
    return f"{ws.title}: added {company} — {title} ({label})"


def record_safe(*args, **kwargs):
    """record() that logs instead of raising, for use from the Discord bot."""
    try:
        return record(*args, **kwargs)
    except Exception as e:
        logging.warning(f"Sheets sync failed: {e}")
        return None
