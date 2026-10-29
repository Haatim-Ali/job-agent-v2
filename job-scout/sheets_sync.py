"""
Mirror applications into the Google Sheets job tracker.

Optional: does nothing unless both env vars are set
  GOOGLE_SERVICE_ACCOUNT_JSON  path to a service-account key file
  JOB_TRACKER_SHEET_ID         the spreadsheet ID (from its URL)
and the sheet is shared with the service account's email as an Editor.

Layout (since 2026-10-29):
  Dashboard     totals, status breakdown, applications per week, companies with several roles
                (all formulas over Applications, so it is always current)
  Applications  one row per position, newest first:
                A Company, B Role, C Status, D Applied, E Updated, F Source, G Location,
                H Posting, I Resume, J Notes, K Roles @ Co (formula)
  Targets       companies Haatim wants to apply to, with how many roles he has applied for there
Several roles at one company are separate rows that share the Company; Roles @ Co counts them
and the Dashboard lists those companies. The other tabs (Consulting, MBA, ...) are never touched.

`python sheets_sync.py --layout` (re)applies all formatting and rebuilds the Dashboard; it never
changes the data rows.
"""

import logging
import os
import re
import sys
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

APPS_TAB = "Applications"
DASHBOARD_TAB = "Dashboard"
TARGETS_TAB = "Targets"
HEADER = ["Company", "Role", "Status", "Applied", "Updated", "Source", "Location",
          "Posting", "Resume", "Notes", "Roles @ Co"]
# K1 holds one array formula for the whole column, so rows never need to write it.
ROLES_AT_CO = '={"Roles @ Co";ARRAYFORMULA(IF(A2:A="","",COUNTIF(A2:A,A2:A)))}'

STATUS_LABELS = {
    "to_apply": "To Apply",
    "applied": "Applied",
    "assessment": "Assessment",
    "phone_screen": "Interviewing",
    "interview": "Interviewing",
    "offer": "Offer",
    "rejected": "Rejected",
    "closed": "Closed",
    "withdrawn": "Withdrawn",
}
# Dropdown order; also the order of the Dashboard breakdown.
STATUS_ORDER = ["To Apply", "Applied", "Assessment", "Interviewing", "Offer", "Rejected", "Closed", "Withdrawn"]
# (background, text) per status chip
STATUS_COLORS = {
    "To Apply": ("#FFF4E5", "#9A4A00"),
    "Applied": ("#E3EEFF", "#1D4ED8"),
    "Assessment": ("#EFE7FF", "#6D28D9"),
    "Interviewing": ("#FFF3C4", "#8A5A00"),
    "Offer": ("#DCF5E7", "#0F7B45"),
    "Rejected": ("#FDE4E4", "#B42318"),
    "Closed": ("#EEEFF2", "#5B6170"),
    "Withdrawn": ("#EEEFF2", "#5B6170"),
}

_SUFFIXES = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|technologies|systems|labs)\b\.?|[.,]", re.I)
_UNKNOWN = {"", "unknown", "unknown company", "not specified", "n/a"}
_LEGACY_NOTE = re.compile(r"^(logged from discord|submitted from packet) [\d/]+$", re.I)


def normalize_company(name):
    return " ".join(_SUFFIXES.sub(" ", str(name).lower()).split())


def normalize_title(title):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", str(title).lower()).split())


def clean_url(url):
    """Drop markdown leftovers like **url** or <url>."""
    return str(url or "").strip().strip("*<> ")


def url_key(url):
    """Compare postings by host + path: tracking/share query strings differ between copies."""
    parts = urlsplit(clean_url(url).lower())
    return f"{parts.netloc}{parts.path.rstrip('/')}" if parts.netloc else ""


def guess_company(url, known):
    """Name an 'Unknown Company' from the posting link, using a company already in the sheet
    (careers.cisco.com → Cisco, nvidia.wd5.myworkdayjobs.com → NVIDIA)."""
    host = urlsplit(clean_url(url).lower()).netloc
    labels = set(re.split(r"[.\-]", host))
    for name in sorted(known, key=len, reverse=True):
        key = normalize_company(name).replace(" ", "")
        if len(key) >= 3 and key in labels:
            return name
    return None


def today():
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%-m/%-d/%Y")


def posting_cell(url):
    url = clean_url(url)
    return f'=HYPERLINK("{url}", "Open ↗")' if url else ""


def posting_url(cell):
    """The link inside a Posting cell (formula or a bare URL)."""
    m = re.match(r'=HYPERLINK\("([^"]*)"', str(cell))
    return m.group(1) if m else str(cell).strip()


def resume_cell(filename, link=None):
    """Resume cell: a clickable 'Tailored ↗' when there's a link to the file, else the given text."""
    if not link:
        return "Default" if "default" in str(filename).lower() else filename
    return f'=HYPERLINK("{link}", "Tailored ↗")'


def _settings():
    # Read at call time, not import time: bot.py imports this module before load_dotenv() runs.
    return os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"), os.getenv("JOB_TRACKER_SHEET_ID")


def enabled():
    return all(_settings())


def _open_sheet():
    import gspread  # imported lazily so the bot runs without it when sync is off
    key_file, sheet_id = _settings()
    return gspread.service_account(filename=key_file).open_by_key(sheet_id)


def _apps_tab(sheet):
    for ws in sheet.worksheets():
        if ws.title == APPS_TAB:
            return ws
    raise RuntimeError(f"no '{APPS_TAB}' tab in the tracker; run `python sheets_sync.py --layout`")


def find_row(rows, company, title, url=""):
    """Row number (1-based) of this application in the Applications rows, or None.
    Match order: same posting (host + path) → same company and role."""
    key = url_key(url)
    target, role = normalize_company(company), normalize_title(title)
    for i, row in enumerate(rows[1:], start=2):
        row = row + [""] * (len(HEADER) - len(row))
        if key and url_key(posting_url(row[7])) == key:
            return i
        if target and normalize_company(row[0]) == target and role and normalize_title(row[1]) == role:
            return i
    return None


def record(company, title, url, status, notes="", sheet=None, resume=None, source="JobAgent", location=""):
    """Create or update the tracker row. Returns a short description of what changed, or None.

    resume: value for the Resume cell (see resume_cell); left untouched when None."""
    if sheet is None:
        if not enabled():
            logging.info("Sheets sync skipped: GOOGLE_SERVICE_ACCOUNT_JSON / JOB_TRACKER_SHEET_ID not set")
            return None
        sheet = _open_sheet()
    ws = _apps_tab(sheet)
    rows = ws.get_all_values(value_render_option="FORMULA")
    url = clean_url(url)
    company, title = str(company).strip(), str(title).strip()
    if company.lower() in _UNKNOWN:
        company = guess_company(url, {r[0] for r in rows[1:] if r and r[0]}) or company or "Unknown"
    if _LEGACY_NOTE.match(notes or ""):
        notes = ""  # the Applied date and Source columns say this now
    label = STATUS_LABELS.get(status, status.replace("_", " ").title())
    row = find_row(rows, company, title, url)

    if row is not None:
        current = (rows[row - 1] + [""] * len(HEADER))[2]
        if status == "applied" and current not in ("", "To Apply"):
            change = "already tracked"
        else:
            ws.update(range_name=f"C{row}", values=[[label]])
            change = f"status → {label}"
        ws.update(range_name=f"E{row}", values=[[today()]], value_input_option="USER_ENTERED")
        if resume:
            ws.update(range_name=f"I{row}", values=[[resume]], value_input_option="USER_ENTERED")
        return f"{APPS_TAB} row {row}: {change}"

    applied = today() if status != "to_apply" else ""
    ws.append_row([company, title, label, applied, today(), source, location,
                   posting_cell(url), resume or "", notes],
                  value_input_option="USER_ENTERED", table_range="A1")
    # Newest first. Sorting (not inserting at row 2) keeps every formula's A2:A references intact.
    ws.sort((4, "des"), (5, "des"), range=f"A2:J{len(rows) + 1}")
    roles = 1 + sum(normalize_company(r[0]) == normalize_company(company) for r in rows[1:] if r)
    extra = f"; {roles} roles there now" if roles > 1 else ""
    return f"{APPS_TAB}: added {company} — {title} ({label}{extra})"


# How far along each status is; an email only moves a row forward (a late "we received your
# application" never undoes a rejection). Rejected/Closed/Withdrawn end it; only an offer beats them.
STATUS_RANK = {"": 0, "To Apply": 0, "Applied": 1, "Assessment": 2, "Interviewing": 3,
               "Rejected": 4, "Closed": 4, "Withdrawn": 4, "Offer": 5}


def same_company(a, b):
    a, b = normalize_company(a), normalize_company(b)
    return bool(a and b) and (a == b or a.startswith(b + " ") or b.startswith(a + " "))


def role_score(a, b):
    from difflib import SequenceMatcher
    a, b = normalize_title(a), normalize_title(b)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def pick_row(rows, company, role, status=""):
    """(row number, None) for the row an email is about; (None, None) when it's about an application
    not in the sheet (a new row); (None, reason) when it can't be told apart from the company's
    other roles, so Haatim decides."""
    cands = [i for i, r in enumerate(rows[1:], start=2) if r and same_company(r[0], company)]
    if not cands:
        return None, None
    scored = sorted(((role_score(rows[i - 1][1], role), i) for i in cands), reverse=True)
    best, row = scored[0]
    clear = len(scored) == 1 or best - scored[1][0] >= 0.1
    if len(cands) == 1 and (status == "applied" or not role or best >= 0.4):
        return row, None  # titles in emails often differ from the posting's (OpenArt, ASML)
    if role and best >= (0.6 if status == "applied" else 0.75) and clear:
        return row, None
    if role and best < 0.4:
        return None, None  # a role at this company that isn't tracked yet
    if status == "applied" and all(STATUS_RANK.get(rows[i - 1][2], 0) >= 1 for i in cands):
        return cands[0], None  # a confirmation can't change any of them; no need to ask
    return None, f"{len(cands)} roles at {rows[cands[0] - 1][0]} in the sheet"


def record_email(company, role, status, when, note, sheet=None):
    """Apply what an email says to the Applications tab. Returns what happened:
    a description, "unchanged", or "ambiguous: <why>" (nothing written; Haatim decides)."""
    if sheet is None:
        sheet = _open_sheet()
    ws = _apps_tab(sheet)
    rows = [r + [""] * (len(HEADER) - len(r)) for r in ws.get_all_values(value_render_option="FORMULA")]
    label = STATUS_LABELS.get(status, status.title())
    row, why = pick_row(rows, company, role, status)
    if why:
        return f"ambiguous: {why}"
    if row is not None:
        current = rows[row - 1]
        if STATUS_RANK.get(label, 0) <= STATUS_RANK.get(current[2], 0):
            return "unchanged"
        notes = f"{current[9]}; {note}" if current[9] else note
        ws.update(range_name=f"C{row}:E{row}", values=[[label, current[3] or (when if status == "applied" else ""), when]],
                  value_input_option="USER_ENTERED")
        ws.update(range_name=f"J{row}", values=[[notes]])
        return f"{current[0]} — {current[1]}: {current[2] or 'blank'} → {label}"
    ws.append_row([company, role or "Role not named in email", label, when if status == "applied" else "", when,
                   "Email", "", "", "", note], value_input_option="USER_ENTERED", table_range="A1")
    ws.sort((4, "des"), (5, "des"), range=f"A2:J{len(rows) + 1}")
    return f"added {company} ({label}) from email"


def record_safe(*args, **kwargs):
    """record() that logs instead of raising, for use from the Discord bot."""
    try:
        result = record(*args, **kwargs)
        if result:
            logging.info(f"Sheets sync: {result}")
        return result
    except Exception as e:
        logging.warning(f"Sheets sync failed: {e}")
        return None


# ── Layout ─────────────────────────────────────────────────────────────────────

FONT = "Roboto"
INK = "#1F2430"
MUTED = "#6B7280"
HEADER_BG = "#1F2A44"
BAND = "#F7F8FA"
LINE = "#E5E7EB"
ACCENT = "#3B5BDB"
WIDTHS = [190, 430, 120, 90, 90, 110, 170, 90, 100, 320, 95]


def _rgb(hex_color):
    h = hex_color.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


def _text(color=INK, bold=False, size=10, italic=False):
    return {"foregroundColor": _rgb(color), "bold": bold, "fontSize": size, "fontFamily": FONT, "italic": italic}


def _grid(sid, r0, r1, c0, c1):
    return {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": c0, "endColumnIndex": c1}


def _fmt(rng, fmt, fields):
    return {"repeatCell": {"range": rng, "cell": {"userEnteredFormat": fmt}, "fields": fields}}


def _ensure_tab(sheet, title, index, rows=1000, cols=26):
    for ws in sheet.worksheets():
        if ws.title == title:
            return ws
    return sheet.add_worksheet(title=title, rows=rows, cols=cols, index=index)


def _clear_requests(sheet, sid, keep_banding=False):
    """Delete this tab's conditional formats, banding and charts so a re-run starts clean."""
    meta = sheet.fetch_sheet_metadata()
    tab = next(s for s in meta["sheets"] if s["properties"]["sheetId"] == sid)
    reqs = [{"deleteConditionalFormatRule": {"sheetId": sid, "index": 0}} for _ in tab.get("conditionalFormats", [])]
    reqs += [{"deleteBanding": {"bandedRangeId": b["bandedRangeId"]}} for b in tab.get("bandedRanges", [])]
    reqs += [{"deleteEmbeddedObject": {"objectId": c["chartId"]}} for c in tab.get("charts", [])]
    return reqs


def _status_rules(rng):
    rules = []
    for status, (bg, fg) in STATUS_COLORS.items():
        rules.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [rng],
            "booleanRule": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": status}]},
                            "format": {"backgroundColor": _rgb(bg), "textFormat": {"foregroundColor": _rgb(fg), "bold": True}}},
        }}})
    return rules


def layout_applications(sheet):
    ws = _apps_tab(sheet)
    sid, n = ws.id, 1000
    ws.update(range_name="A1:J1", values=[HEADER[:10]])
    ws.update(range_name="K1", values=[[ROLES_AT_CO]], value_input_option="USER_ENTERED")
    reqs = _clear_requests(sheet, sid)
    reqs += [
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {
            "frozenRowCount": 1, "frozenColumnCount": 1, "hideGridlines": True}, "tabColorStyle": {"rgbColor": _rgb(ACCENT)}},
            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount,gridProperties.hideGridlines,tabColorStyle"}},
        _fmt(_grid(sid, 0, n, 0, 11), {"textFormat": _text(), "verticalAlignment": "MIDDLE", "wrapStrategy": "CLIP",
                                        "padding": {"left": 8, "right": 8, "top": 4, "bottom": 4}},
             "userEnteredFormat(textFormat,verticalAlignment,wrapStrategy,padding)"),
        _fmt(_grid(sid, 0, 1, 0, 11), {"backgroundColor": _rgb(HEADER_BG), "textFormat": _text("#FFFFFF", bold=True),
                                        "verticalAlignment": "MIDDLE", "horizontalAlignment": "LEFT"},
             "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,horizontalAlignment)"),
        _fmt(_grid(sid, 1, n, 0, 1), {"textFormat": _text(bold=True)}, "userEnteredFormat.textFormat.bold"),
        _fmt(_grid(sid, 1, n, 2, 3), {"horizontalAlignment": "CENTER"}, "userEnteredFormat.horizontalAlignment"),
        _fmt(_grid(sid, 1, n, 3, 5), {"numberFormat": {"type": "DATE", "pattern": "mmm d"}, "horizontalAlignment": "CENTER"},
             "userEnteredFormat(numberFormat,horizontalAlignment)"),
        _fmt(_grid(sid, 1, n, 5, 7), {"textFormat": _text(MUTED)}, "userEnteredFormat.textFormat"),
        _fmt(_grid(sid, 1, n, 7, 9), {"horizontalAlignment": "CENTER", "textFormat": _text(ACCENT)},
             "userEnteredFormat(horizontalAlignment,textFormat)"),
        _fmt(_grid(sid, 1, n, 9, 10), {"textFormat": _text(MUTED, size=9)}, "userEnteredFormat.textFormat"),
        _fmt(_grid(sid, 1, n, 10, 11), {"horizontalAlignment": "CENTER", "textFormat": _text(MUTED)},
             "userEnteredFormat(horizontalAlignment,textFormat)"),
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                                       "properties": {"pixelSize": 36}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 1, "endIndex": n},
                                       "properties": {"pixelSize": 30}, "fields": "pixelSize"}},
        {"setDataValidation": {"range": _grid(sid, 1, n, 2, 3), "rule": {
            "condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": s} for s in STATUS_ORDER]},
            "showCustomUi": True, "strict": False}}},
        {"addBanding": {"bandedRange": {"range": _grid(sid, 0, n, 0, 11), "rowProperties": {
            "headerColor": _rgb(HEADER_BG), "firstBandColor": _rgb("#FFFFFF"), "secondBandColor": _rgb(BAND)}}}},
        {"setBasicFilter": {"filter": {"range": _grid(sid, 0, n, 0, 11)}}},
    ]
    reqs += [{"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                                            "properties": {"pixelSize": w}, "fields": "pixelSize"}} for i, w in enumerate(WIDTHS)]
    reqs += _status_rules(_grid(sid, 1, n, 2, 3))
    # Muted rows for finished applications
    reqs.append({"addConditionalFormatRule": {"index": len(STATUS_COLORS), "rule": {
        "ranges": [_grid(sid, 1, n, 0, 2)],
        "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": '=OR($C2="Rejected",$C2="Closed",$C2="Withdrawn")'}]},
                        "format": {"textFormat": {"foregroundColor": _rgb("#9CA3AF")}}}}}})
    sheet.batch_update({"requests": reqs})


A = f"'{APPS_TAB}'!"
ACTIVE = f'COUNTIFS({A}C2:C,"<>To Apply",{A}A2:A,"<>")'


def dashboard_cells():
    """(cell, value) pairs for the Dashboard: every number is a formula over Applications."""
    cells = [
        ("B2", "Internship Tracker"),
        ("B3", f'="Updated "&TEXT(MAX({A}E2:E),"mmm d, yyyy")&"  ·  "&COUNTIF({A}C2:C,"To Apply")&" saved to apply later"'),
        # KPI cards: label row 5, number row 6, caption row 7
        ("B5", "APPLICATIONS"), ("B6", f"={ACTIVE}"), ("B7", f'=COUNTIFS({A}D2:D,">="&TODAY()-6)&" this week"'),
        ("D5", "COMPANIES"), ("D6", f'=IFERROR(COUNTUNIQUE(FILTER({A}A2:A,{A}A2:A<>"",{A}C2:C<>"To Apply")),0)'),
        ("D7", f'=IFERROR(ROWS(UNIQUE(FILTER({A}A2:A,{A}K2:K>1))),0)&" with several roles"'),
        ("F5", "IN PROGRESS"), ("F6", f'=COUNTIF({A}C2:C,"Applied")+COUNTIF({A}C2:C,"Assessment")+COUNTIF({A}C2:C,"Interviewing")'),
        ("F7", f'=COUNTIF({A}C2:C,"Assessment")&" assessments · "&COUNTIF({A}C2:C,"Interviewing")&" interviews"'),
        ("H5", "RESPONSE RATE"),
        ("H6", f'=IFERROR((COUNTIF({A}C2:C,"Assessment")+COUNTIF({A}C2:C,"Interviewing")+COUNTIF({A}C2:C,"Offer")+COUNTIF({A}C2:C,"Rejected"))/{ACTIVE},0)'),
        ("H7", "heard back (any answer)"),
        ("J5", "REJECTED"), ("J6", f'=COUNTIF({A}C2:C,"Rejected")'),
        ("J7", f'=COUNTIF({A}C2:C,"Closed")&" closed / expired"'),
        ("L5", "OFFERS"), ("L6", f'=COUNTIF({A}C2:C,"Offer")'), ("L7", "keep going"),
        # Status breakdown
        ("B10", "By status"), ("C10", "Count"), ("D10", ""),
    ]
    for i, status in enumerate(STATUS_ORDER):
        r = 11 + i
        bar = STATUS_COLORS[status][1]
        cells += [(f"B{r}", status), (f"C{r}", f'=COUNTIF({A}C2:C,B{r})'),
                  (f"D{r}", f'=SPARKLINE(C{r},{{"charttype","bar";"max",MAX(1,COUNTA({A}A2:A));"color1","{bar}"}})')]
    # Applications per week, last 10 weeks (weeks start Monday)
    cells += [("F10", "Week of"), ("G10", "Applied")]
    for i in range(10):
        r = 11 + i
        cells += [(f"F{r}", f"=TODAY()-WEEKDAY(TODAY(),3)-7*{9 - i}"),
                  (f"G{r}", f'=COUNTIFS({A}D2:D,">="&F{r},{A}D2:D,"<"&F{r}+7)')]
    # Companies with several roles, and recent activity
    cells += [
        ("B22", "Companies with several roles"), ("C22", "Roles"),
        ("B23", f'=IFERROR(QUERY(QUERY({A}A2:A,"select A, count(A) where A <> \'\' group by A label count(A) \'\'",0),'
                f'"select Col1, Col2 where Col2 > 1 order by Col2 desc, Col1",0),"None yet")'),
    ]
    # Recent activity beside it: the role (G) overflows across the empty H:J
    def recent(col):
        return (f'=IFERROR(QUERY({{{A}A2:A,{A}B2:B,{A}C2:C,{A}E2:E}},'
                f'"select Col{col} where Col1 <> \'\' order by Col4 desc limit 14",0),"")')
    cells += [("F22", "Recent activity"), ("L22", "Status"), ("M22", "Updated")]
    cells += [(c, recent(n)) for c, n in (("F23", 1), ("G23", 2), ("L23", 3), ("M23", 4))]
    return cells


def layout_dashboard(sheet):
    ws = _ensure_tab(sheet, DASHBOARD_TAB, 0, rows=60, cols=14)
    sid = ws.id
    ws.batch_clear(["A1:N60"])
    ws.batch_update([{"range": c, "values": [[v]]} for c, v in dashboard_cells()], value_input_option="USER_ENTERED")
    reqs = _clear_requests(sheet, sid)
    widths = [24, 240, 70, 130, 24, 150, 70, 150, 80, 150, 24, 150, 80, 24]
    reqs += [{"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                                            "properties": {"pixelSize": w}, "fields": "pixelSize"}} for i, w in enumerate(widths)]
    reqs += [
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"hideGridlines": True},
                                                  "tabColorStyle": {"rgbColor": _rgb(HEADER_BG)}},
                                   "fields": "gridProperties.hideGridlines,tabColorStyle"}},
        _fmt(_grid(sid, 0, 60, 0, 14), {"backgroundColor": _rgb("#FFFFFF"), "textFormat": _text(), "verticalAlignment": "MIDDLE",
                                         "wrapStrategy": "CLIP"},
             "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,wrapStrategy)"),
        _fmt(_grid(sid, 1, 2, 1, 12), {"textFormat": _text(INK, bold=True, size=20)}, "userEnteredFormat.textFormat"),
        _fmt(_grid(sid, 2, 3, 1, 12), {"textFormat": _text(MUTED, size=10)}, "userEnteredFormat.textFormat"),
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 1, "endIndex": 2},
                                       "properties": {"pixelSize": 44}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 5, "endIndex": 6},
                                       "properties": {"pixelSize": 52}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 9, "endIndex": 60},
                                       "properties": {"pixelSize": 24}, "fields": "pixelSize"}},
    ]
    # KPI cards sit in B, D, F, H, J, L (the columns between are gaps or table columns)
    card_bg = "#F4F6FB"
    for c in (1, 3, 5, 7, 9, 11):
        reqs += [
            _fmt(_grid(sid, 4, 7, c, c + 1), {"backgroundColor": _rgb(card_bg), "horizontalAlignment": "LEFT",
                                              "padding": {"left": 12, "right": 8, "top": 2, "bottom": 2}},
                 "userEnteredFormat(backgroundColor,horizontalAlignment,padding)"),
            _fmt(_grid(sid, 4, 5, c, c + 1), {"textFormat": _text(MUTED, bold=True, size=8)}, "userEnteredFormat.textFormat"),
            _fmt(_grid(sid, 5, 6, c, c + 1), {"textFormat": _text(INK, bold=True, size=24), "numberFormat": {"type": "NUMBER", "pattern": "0"}},
                 "userEnteredFormat(textFormat,numberFormat)"),
            _fmt(_grid(sid, 6, 7, c, c + 1), {"textFormat": _text(MUTED, size=9), "wrapStrategy": "OVERFLOW_CELL"},
                 "userEnteredFormat(textFormat,wrapStrategy)"),
            {"updateBorders": {"range": _grid(sid, 4, 7, c, c + 1), "left": {"style": "SOLID_THICK", "colorStyle": {"rgbColor": _rgb(ACCENT)}}}},
        ]
    reqs.append(_fmt(_grid(sid, 5, 6, 7, 8), {"numberFormat": {"type": "PERCENT", "pattern": "0%"}}, "userEnteredFormat.numberFormat"))
    reqs.append(_fmt(_grid(sid, 5, 6, 9, 10), {"textFormat": _text("#B42318", bold=True, size=24)}, "userEnteredFormat.textFormat"))
    reqs.append(_fmt(_grid(sid, 5, 6, 11, 12), {"textFormat": _text("#0F7B45", bold=True, size=24)}, "userEnteredFormat.textFormat"))
    # Table headers (row 10 and 22)
    for r, c0, c1 in ((9, 1, 4), (9, 5, 7), (21, 1, 3), (21, 5, 13)):
        if True:
            reqs.append(_fmt(_grid(sid, r, r + 1, c0, c1), {"textFormat": _text(MUTED, bold=True, size=9)}, "userEnteredFormat.textFormat"))
            reqs.append({"updateBorders": {"range": _grid(sid, r, r + 1, c0, c1), "bottom": {"style": "SOLID", "colorStyle": {"rgbColor": _rgb(LINE)}}}})
    reqs.append(_fmt(_grid(sid, 10, 20, 5, 6), {"numberFormat": {"type": "DATE", "pattern": "mmm d"}}, "userEnteredFormat.numberFormat"))
    reqs.append(_fmt(_grid(sid, 22, 60, 12, 13), {"numberFormat": {"type": "DATE", "pattern": "mmm d"}, "textFormat": _text(MUTED),
                                                   "horizontalAlignment": "CENTER"},
                     "userEnteredFormat(numberFormat,textFormat,horizontalAlignment)"))
    reqs.append(_fmt(_grid(sid, 22, 60, 1, 2), {"textFormat": _text(bold=True)}, "userEnteredFormat.textFormat"))
    reqs.append(_fmt(_grid(sid, 22, 60, 5, 6), {"textFormat": _text(bold=True)}, "userEnteredFormat.textFormat"))
    reqs.append(_fmt(_grid(sid, 22, 60, 6, 7), {"wrapStrategy": "OVERFLOW_CELL", "textFormat": _text(MUTED)},
                     "userEnteredFormat(wrapStrategy,textFormat)"))
    reqs.append(_fmt(_grid(sid, 22, 60, 11, 12), {"horizontalAlignment": "CENTER"}, "userEnteredFormat.horizontalAlignment"))
    reqs.append(_fmt(_grid(sid, 22, 60, 2, 3), {"horizontalAlignment": "CENTER"}, "userEnteredFormat.horizontalAlignment"))
    reqs.append(_fmt(_grid(sid, 1, 3, 1, 12), {"wrapStrategy": "OVERFLOW_CELL"}, "userEnteredFormat.wrapStrategy"))
    reqs += _status_rules(_grid(sid, 10, 19, 1, 2))
    reqs += _status_rules(_grid(sid, 22, 60, 11, 12))
    # Weekly chart beside the week table (the status table has its own coloured bars)
    reqs.append({"addChart": {"chart": {"spec": {
        "title": "Applications per week", "titleTextFormat": _text(INK, bold=True, size=11), "fontName": FONT,
        "basicChart": {"chartType": "COLUMN", "legendPosition": "NO_LEGEND",
                       "axis": [{"position": "BOTTOM_AXIS"}, {"position": "LEFT_AXIS"}],
                       "domains": [{"domain": {"sourceRange": {"sources": [_grid(sid, 10, 20, 5, 6)]}}}],
                       "series": [{"series": {"sourceRange": {"sources": [_grid(sid, 10, 20, 6, 7)]}},
                                   "targetAxis": "LEFT_AXIS", "colorStyle": {"rgbColor": _rgb(ACCENT)}}]}},
        "position": {"overlayPosition": {"anchorCell": {"sheetId": sid, "rowIndex": 8, "columnIndex": 7},
                                         "offsetXPixels": 16, "widthPixels": 540, "heightPixels": 270}}}}})
    sheet.batch_update({"requests": reqs})


def layout_targets(sheet):
    """Targets: A Company (typed by hand), B Roles applied (formula), C Latest status (formula)."""
    ws = next((w for w in sheet.worksheets() if w.title == TARGETS_TAB), None)
    if ws is None:
        return
    sid, n = ws.id, max(ws.row_count, 200)
    ws.update(range_name="A1:C1", values=[["Company", "Roles applied", "Latest status"]])
    ws.update(range_name="B2:C2", values=[[
        f'=ARRAYFORMULA(IF(A2:A="","",COUNTIF({A}A2:A,A2:A)))',
        f'=ARRAYFORMULA(IF(A2:A="","",IFERROR(VLOOKUP(A2:A,{A}A2:C,3,FALSE),"Not yet")))',
    ]], value_input_option="USER_ENTERED")
    reqs = _clear_requests(sheet, sid)
    reqs += [
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1, "hideGridlines": True},
                                                  "tabColorStyle": {"rgbColor": _rgb("#9CA3AF")}},
                                   "fields": "gridProperties.frozenRowCount,gridProperties.hideGridlines,tabColorStyle"}},
        _fmt(_grid(sid, 0, n, 0, 3), {"textFormat": _text(), "verticalAlignment": "MIDDLE", "padding": {"left": 8, "right": 8}},
             "userEnteredFormat(textFormat,verticalAlignment,padding)"),
        _fmt(_grid(sid, 0, 1, 0, 3), {"backgroundColor": _rgb(HEADER_BG), "textFormat": _text("#FFFFFF", bold=True)},
             "userEnteredFormat(backgroundColor,textFormat)"),
        _fmt(_grid(sid, 1, n, 1, 3), {"horizontalAlignment": "CENTER"}, "userEnteredFormat.horizontalAlignment"),
        {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 0, "endIndex": n},
                                       "properties": {"pixelSize": 28}, "fields": "pixelSize"}},
        {"addBanding": {"bandedRange": {"range": _grid(sid, 0, n, 0, 3), "rowProperties": {
            "headerColor": _rgb(HEADER_BG), "firstBandColor": _rgb("#FFFFFF"), "secondBandColor": _rgb(BAND)}}}},
    ]
    reqs += [{"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                                            "properties": {"pixelSize": w}, "fields": "pixelSize"}} for i, w in enumerate([240, 120, 140])]
    reqs += _status_rules(_grid(sid, 1, n, 2, 3))
    reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {
        "ranges": [_grid(sid, 1, n, 2, 3)],
        "booleanRule": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": "Not yet"}]},
                        "format": {"textFormat": {"foregroundColor": _rgb("#9CA3AF"), "italic": True}}}}}})
    sheet.batch_update({"requests": reqs})


def apply_layout(sheet=None):
    sheet = sheet or _open_sheet()
    layout_applications(sheet)
    layout_targets(sheet)
    layout_dashboard(sheet)


if __name__ == "__main__":
    if "--layout" in sys.argv:
        from dotenv import load_dotenv
        load_dotenv(".env")
        apply_layout()
        print("Tracker layout applied.")
