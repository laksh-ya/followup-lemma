#input_type_name: ReplyInput
#output_type_name: ReplyResult
#function_name: handle_reply

"""Handle an inbound customer reply: resolve the customer by email (same identity key
as everything else), classify the message, log it, and apply the right side effect —
promise-to-pay -> promises row + pause escalation; dispute -> DISPUTED; paid-claim ->
PAUSED for reconciliation. Used by the app's "Log reply" and by the production Gmail
inbound workflow. Classification here is deterministic for reliability; the
reply-triager agent is used on the live mailbox path.
"""

from datetime import date, timedelta
from typing import Optional

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


class ReplyInput(BaseModel):
    body: str
    invoice_id: Optional[str] = None
    from_email: Optional[str] = None


class ReplyResult(BaseModel):
    ok: bool
    category: str
    invoice_no: str
    detail: str


def _classify(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ["already paid", "have paid", "we paid", "payment done", "utr", "transaction id", "neft ref"]):
        return "PAID_CLAIM"
    if any(k in t for k in ["dispute", "incorrect", "wrong amount", "not agree", "disagree", "overcharged", "error in"]):
        return "DISPUTE"
    if any(k in t for k in ["will pay", "we'll pay", "by friday", "by monday", "next week", "by end of", "clear this by", "settle by", "pay by", "promise"]):
        return "PROMISE_TO_PAY"
    if "?" in t or any(k in t for k in ["resend", "payment link", "how do", "can you", "could you", "where do"]):
        return "QUESTION"
    return "OTHER"


async def handle_reply(ctx: FunctionContext, data: ReplyInput) -> ReplyResult:
    pod = Pod.from_env()
    inv = None
    if data.invoice_id:
        inv = pod.table("invoices").get(data.invoice_id)
    elif data.from_email:
        en = data.from_email.strip().lower()
        clients = pod.records.list("clients", limit=2000).to_dict()["items"]
        cid = next((c["id"] for c in clients if (c.get("email_norm") or c.get("email", "").lower()) == en), None)
        if cid:
            invs = pod.records.list("invoices", limit=2000, filter=[
                {"field": "client_id", "op": "eq", "value": cid},
                {"field": "status", "op": "eq", "value": "ACTIVE"}]).to_dict()["items"]
            invs.sort(key=lambda x: int(x.get("days_overdue") or 0), reverse=True)
            inv = invs[0] if invs else None
    if not inv:
        return ReplyResult(ok=False, category="OTHER", invoice_no="", detail="no matching invoice/customer")

    category = _classify(data.body)
    client = pod.table("clients").get(str(inv["client_id"])) if inv.get("client_id") else {}
    no = inv.get("invoice_no", "")

    pod.table("interactions").create({
        "invoice_id": inv["id"], "client_id": inv.get("client_id"),
        "kind": "REPLY_RECEIVED", "channel": "EMAIL", "direction": "INBOUND",
        "summary": f"Reply from {(client or {}).get('name','customer')} ({category.replace('_',' ').title()}): {data.body[:80]}",
        "detail": {"category": category, "from": (client or {}).get("email"), "body": data.body[:1000]},
        "actor_label": "customer", "level": "INFO"})

    detail = "logged"
    if category == "PROMISE_TO_PAY":
        pod.table("promises").create({"invoice_id": inv["id"], "client_id": inv.get("client_id"),
            "promised_date": (date.today() + timedelta(days=5)).isoformat(), "status": "OPEN", "source": "reply"})
        pod.table("interactions").create({"invoice_id": inv["id"], "client_id": inv.get("client_id"),
            "kind": "PROMISE_MADE", "channel": "SYSTEM", "direction": "INTERNAL",
            "summary": f"Promise-to-pay recorded for {no}; escalation paused", "actor_label": "reply", "level": "INFO"})
        detail = "promise recorded"
    elif category == "DISPUTE":
        pod.table("invoices").update(inv["id"], {"status": "DISPUTED"})
        pod.table("interactions").create({"invoice_id": inv["id"], "client_id": inv.get("client_id"),
            "kind": "STATUS_CHANGE", "channel": "SYSTEM", "direction": "INTERNAL",
            "summary": f"{no} set DISPUTED from reply — follow-ups paused", "actor_label": "reply", "level": "WARN"})
        detail = "marked disputed"
    elif category == "PAID_CLAIM":
        pod.table("invoices").update(inv["id"], {"status": "PAUSED", "notes": "Customer claims paid — verify"})
        pod.table("interactions").create({"invoice_id": inv["id"], "client_id": inv.get("client_id"),
            "kind": "STATUS_CHANGE", "channel": "SYSTEM", "direction": "INTERNAL",
            "summary": f"{no} paused — customer claims paid; awaiting reconciliation", "actor_label": "reply", "level": "INFO"})
        detail = "paused for reconciliation"

    return ReplyResult(ok=True, category=category, invoice_no=no, detail=detail)
