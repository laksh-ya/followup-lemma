#input_type_name: IngestInput
#output_type_name: IngestResult
#function_name: ingest_invoices

"""Ingest invoices from CSV upload, manual entry, or seeding — and TRACK THE SAME
CUSTOMER across all of them. Customer identity is the normalized email
(lowercased/trimmed) stored on clients.email_norm (unique). Every row is matched to an
existing client by that key or creates a new one, so one customer is exactly one
record no matter how many invoices or uploads reference them. Invoices upsert by
invoice_no. Stage/risk are left to classify_and_score.
"""

from datetime import date
from typing import Optional

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod

_BAD = ["ignore previous", "forget instructions", "system:", "assistant:", "<|", "|>", "{{", "}}"]


class InvoiceRow(BaseModel):
    invoice_no: str
    client_name: str
    client_email: str
    amount: float
    currency: str = "INR"
    due_date: str
    payment_link: Optional[str] = None
    status: str = "ACTIVE"
    company: Optional[str] = None
    phone: Optional[str] = None
    vip: Optional[bool] = None
    payment_behavior: Optional[str] = None
    notes: Optional[str] = None


class IngestInput(BaseModel):
    rows: list[InvoiceRow]


class IngestResult(BaseModel):
    clients_created: int
    clients_matched: int
    invoices_created: int
    invoices_updated: int
    errors: list[str]


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def _clean(s: str) -> str:
    low = (s or "").lower()
    for p in _BAD:
        if p in low:
            raise ValueError("forbidden pattern in text")
    return (s or "").strip()


async def ingest_invoices(ctx: FunctionContext, data: IngestInput) -> IngestResult:
    pod = Pod.from_env()

    clients = pod.records.list("clients", limit=2000).to_dict()["items"]
    by_email = {}
    for c in clients:
        key = c.get("email_norm") or _norm(c.get("email", ""))
        if key:
            by_email[key] = c["id"]

    invoices = pod.records.list("invoices", limit=2000).to_dict()["items"]
    by_no = {str(i.get("invoice_no")): i["id"] for i in invoices}

    cc = cm = ic = iu = 0
    errors = []

    for idx, row in enumerate(data.rows):
        try:
            email = _norm(row.client_email)
            if not email or "@" not in email:
                raise ValueError("invalid email")
            amount = float(row.amount)
            if amount <= 0 or amount > 10_000_000:
                raise ValueError("amount out of range")
            due = date.fromisoformat(str(row.due_date)[:10])

            cid = by_email.get(email)
            if not cid:
                cfields = {
                    "name": _clean(row.client_name)[:200],
                    "email": row.client_email.strip()[:320],
                    "email_norm": email[:320],
                    "payment_behavior": row.payment_behavior or "AVERAGE",
                }
                if row.company: cfields["company"] = row.company[:200]
                if row.phone: cfields["phone"] = row.phone[:40]
                if row.vip is not None: cfields["vip"] = bool(row.vip)
                if row.notes: cfields["notes"] = row.notes[:2000]
                created = pod.table("clients").create(cfields)
                cid = created["id"]
                by_email[email] = cid
                cc += 1
            else:
                cm += 1

            ifields = {
                "invoice_no": row.invoice_no.strip()[:40],
                "client_id": cid,
                "amount": round(amount, 2),
                "currency": (row.currency or "INR")[:3],
                "due_date": due.isoformat(),
                "status": (row.status or "ACTIVE").upper(),
            }
            if row.payment_link: ifields["payment_link"] = row.payment_link[:500]

            existing = by_no.get(row.invoice_no.strip())
            if existing:
                pod.table("invoices").update(existing, ifields)
                iu += 1
            else:
                created = pod.table("invoices").create(ifields)
                by_no[row.invoice_no.strip()] = created["id"]
                ic += 1
        except Exception as exc:
            errors.append(f"row {idx} ({getattr(row, 'invoice_no', '?')}): {exc}")

    pod.table("interactions").create({
        "kind": "INVOICE_INGESTED", "channel": "SYSTEM", "direction": "INTERNAL",
        "summary": f"Ingested {ic+iu} invoice(s): {cc} new customer(s), {cm} matched to existing",
        "detail": {"clients_created": cc, "clients_matched": cm, "invoices_created": ic, "invoices_updated": iu, "errors": errors[:10]},
        "actor_user": ctx.user_id, "actor_label": "ingest", "level": "INFO" if not errors else "WARN",
    })

    return IngestResult(clients_created=cc, clients_matched=cm, invoices_created=ic, invoices_updated=iu, errors=errors)
