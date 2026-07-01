#input_type_name: SheetInput
#output_type_name: SheetResult
#function_name: ingest_from_sheet

"""Read invoice rows from a Google Sheet (via the google_sheets connector, on the
invoking user's connected Google account) and return them mapped to the shape
ingest_invoices expects. The app then feeds these rows straight into ingest_invoices —
so customer dedup, validation, and demo=false all reuse the one ingest path.

Header row is required; only invoice_no, client_email, due_date, amount are mandatory.
Accepted header aliases mirror the CSV importer (client/name, email, due, …).
"""

from typing import Optional

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


class SheetInput(BaseModel):
    spreadsheet_id: str
    sheet_range: str = "A1:M500"   # A1 notation; include the header row


class SheetRow(BaseModel):
    invoice_no: str
    client_name: str = ""
    client_email: str = ""
    amount: float = 0
    currency: str = "INR"
    due_date: str = ""
    payment_link: str = ""
    status: str = "ACTIVE"
    company: str = ""
    phone: str = ""
    vip: bool = False
    payment_behavior: Optional[str] = None
    notes: str = ""


class SheetResult(BaseModel):
    ok: bool
    rows: list[SheetRow]
    total_rows: int
    skipped: int
    detail: str


def _num(v):
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except Exception:
        return 0.0


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "y")


async def ingest_from_sheet(ctx: FunctionContext, data: SheetInput) -> SheetResult:
    pod = Pod.from_env()
    sid = (data.spreadsheet_id or "").strip()
    # Accept a full URL and pull out the id between /d/ and the next /
    if "/d/" in sid:
        sid = sid.split("/d/", 1)[1].split("/", 1)[0]
    if not sid:
        return SheetResult(ok=False, rows=[], total_rows=0, skipped=0, detail="Provide a spreadsheet id or URL.")

    try:
        res = pod.connectors.execute("workspace-sheets", "spreadsheets_values_get",
                                     {"spreadsheet_id": sid, "range": data.sheet_range}).to_dict()
        values = (res.get("result") or {}).get("values") or []
    except Exception as exc:
        return SheetResult(ok=False, rows=[], total_rows=0, skipped=0,
                           detail="Couldn't read the sheet — connect a Google account first (Data ▸ setup guide). " + str(exc)[:200])

    if not values:
        return SheetResult(ok=False, rows=[], total_rows=0, skipped=0, detail="No rows found in that range.")

    header = [str(h).strip().lower() for h in values[0]]

    def col(row, *names):
        for n in names:
            if n in header:
                i = header.index(n)
                if i < len(row):
                    return str(row[i]).strip()
        return ""

    rows, skipped = [], 0
    for raw in values[1:]:
        if not any(str(c).strip() for c in raw):
            continue
        r = SheetRow(
            invoice_no=col(raw, "invoice_no", "invoice", "invoice #"),
            client_name=col(raw, "client_name", "client", "name", "customer"),
            client_email=col(raw, "client_email", "email"),
            amount=_num(col(raw, "amount", "value")),
            currency=col(raw, "currency") or "INR",
            due_date=col(raw, "due_date", "due")[:10],
            payment_link=col(raw, "payment_link", "link"),
            status=(col(raw, "status") or "ACTIVE").upper(),
            company=col(raw, "company"),
            phone=col(raw, "phone"),
            vip=_truthy(col(raw, "vip")),
            payment_behavior=(col(raw, "payment_behavior") or "").upper() or None,
            notes=col(raw, "notes"),
        )
        if r.invoice_no and r.client_email and r.due_date:
            rows.append(r)
        else:
            skipped += 1

    return SheetResult(ok=True, rows=rows, total_rows=len(values) - 1, skipped=skipped,
                       detail=f"Read {len(rows)} valid row(s) from the sheet" + (f", {skipped} skipped" if skipped else "") + ".")
