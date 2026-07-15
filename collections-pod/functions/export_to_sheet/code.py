#input_type_name: ExportToSheetInput
#output_type_name: ExportToSheetResult
#function_name: export_to_sheet

"""Write pod data out to a Google Sheet (via the google_sheets connector, on the
invoking user's connected Google account) — the write half of the read-only
ingest_from_sheet, so the sheet can be a live two-way mirror of the pod.

mode="overwrite" (default) replaces the header + all rows starting at sheet_range
(e.g. "Sheet1!A1") — use for a fresh snapshot. mode="append" adds rows after the
existing table without touching the header — use for a growing log (e.g. the
interactions audit trail) without re-writing everything each run.

Uses the COMPOSIO google_sheets connector's GOOGLESHEETS_VALUES_UPDATE (overwrite)
and GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND (append) operations. The sheet must be
shared with (or created by) the connected Google account.
"""

from typing import Literal

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod

_COLUMNS = {
    "invoices": ["invoice_no", "amount", "currency", "due_date", "days_overdue",
                 "stage", "status", "risk_level", "followup_count", "last_followup_at"],
    "drafts": ["invoice_id", "stage", "tone", "channel", "subject", "status",
               "confidence", "risk_level", "fallback_used", "sent_at"],
    "interactions": ["created_at", "invoice_id", "kind", "channel", "direction",
                      "summary", "actor_label", "level"],
}


class ExportToSheetInput(BaseModel):
    spreadsheet_id: str
    source: Literal["invoices", "drafts", "interactions"]
    sheet_range: str = "Sheet1!A1"          # top-left cell for overwrite; table range for append
    mode: Literal["overwrite", "append"] = "overwrite"
    limit: int = 1000


class ExportToSheetResult(BaseModel):
    ok: bool
    rows_written: int
    detail: str


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    return str(v)


async def export_to_sheet(ctx: FunctionContext, data: ExportToSheetInput) -> ExportToSheetResult:
    pod = Pod.from_env()
    sid = (data.spreadsheet_id or "").strip()
    if "/d/" in sid:
        sid = sid.split("/d/", 1)[1].split("/", 1)[0]
    if not sid:
        return ExportToSheetResult(ok=False, rows_written=0, detail="Provide a spreadsheet id or URL.")

    cols = _COLUMNS[data.source]
    items = pod.records.list(data.source, limit=data.limit).to_dict()["items"]
    body_rows = [[_cell(item.get(c)) for c in cols] for item in items]

    try:
        if data.mode == "append":
            values = body_rows
            res = pod.connectors.execute("google_sheets", "GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND", {
                "spreadsheetId": sid,
                "range": data.sheet_range,
                "values": values,
                "majorDimension": "ROWS",
                "valueInputOption": "RAW",
                "insertDataOption": "INSERT_ROWS",
            }).to_dict()
        else:
            values = [cols] + body_rows
            res = pod.connectors.execute("google_sheets", "GOOGLESHEETS_VALUES_UPDATE", {
                "spreadsheet_id": sid,
                "range": data.sheet_range,
                "values": values,
                "major_dimension": "ROWS",
                "value_input_option": "RAW",
            }).to_dict()
    except Exception as exc:
        return ExportToSheetResult(ok=False, rows_written=0,
                                   detail="Couldn't write to the sheet — connect a Google account first (Setup guide). " + str(exc)[:200])

    result = res.get("result") or {}
    if result.get("successful") is False:
        return ExportToSheetResult(ok=False, rows_written=0, detail=str(result.get("error") or "Sheet write failed.")[:300])

    return ExportToSheetResult(ok=True, rows_written=len(body_rows),
                               detail=f"Wrote {len(body_rows)} row(s) of {data.source} to the sheet ({data.mode}).")
