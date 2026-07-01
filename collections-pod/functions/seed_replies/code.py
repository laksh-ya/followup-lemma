#input_type_name: SeedRepliesInput
#output_type_name: SeedRepliesResult
#function_name: seed_replies

"""Seed realistic inbound customer replies so the Replies tab is populated for a demo.
Each reply is tied to a tracked customer (by client_id) and an invoice, classified like
the live reply-triager would, and drives the right side effect: a promise-to-pay
creates a promises row, a dispute sets the invoice DISPUTED, a paid-claim flags it for
verification. Demonstrates the closed loop without a live mailbox.
"""

from datetime import date, timedelta

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


class SeedRepliesInput(BaseModel):
    pass


class SeedRepliesResult(BaseModel):
    replies: int


REPLIES = [
    ("INV-2025-002", "PROMISE_TO_PAY", "Apologies for the delay — we'll clear this by Friday.", 5),
    ("INV-2025-006", "PAID_CLAIM", "We've already paid this last week, please check. UTR 99812.", None),
    ("INV-2025-004", "DISPUTE", "We dispute two line items on this invoice; details attached.", None),
    ("INV-2025-001", "QUESTION", "Can you resend the payment link? The old one expired.", None),
]


async def seed_replies(ctx: FunctionContext, data: SeedRepliesInput) -> SeedRepliesResult:
    pod = Pod.from_env()
    invs = pod.records.list("invoices", limit=2000).to_dict()["items"]
    by_no = {str(i.get("invoice_no")): i for i in invs}

    n = 0
    for no, category, body, promise_days in REPLIES:
        inv = by_no.get(no)
        if not inv:
            continue
        client = pod.table("clients").get(str(inv["client_id"])) if inv.get("client_id") else {}
        frm = (client or {}).get("email", "")
        pod.table("interactions").create({
            "invoice_id": inv["id"], "client_id": inv.get("client_id"),
            "kind": "REPLY_RECEIVED", "channel": "EMAIL", "direction": "INBOUND",
            "summary": f"Reply from {(client or {}).get('name','customer')} ({category.replace('_',' ').title()}): {body[:80]}",
            "detail": {"category": category, "from": frm, "body": body},
            "actor_label": "customer", "level": "INFO",
        })
        n += 1
        if category == "PROMISE_TO_PAY":
            pod.table("promises").create({
                "invoice_id": inv["id"], "client_id": inv.get("client_id"),
                "promised_date": (date.today() + timedelta(days=promise_days or 5)).isoformat(),
                "status": "OPEN", "source": "reply",
            })
            pod.table("interactions").create({
                "invoice_id": inv["id"], "client_id": inv.get("client_id"),
                "kind": "PROMISE_MADE", "channel": "SYSTEM", "direction": "INTERNAL",
                "summary": f"Promise-to-pay recorded for {no} (due in {promise_days or 5} days)",
                "actor_label": "reply-triager", "level": "INFO",
            })
        elif category == "DISPUTE":
            pod.table("invoices").update(inv["id"], {"status": "DISPUTED"})
            pod.table("interactions").create({
                "invoice_id": inv["id"], "client_id": inv.get("client_id"),
                "kind": "STATUS_CHANGE", "channel": "SYSTEM", "direction": "INTERNAL",
                "summary": f"{no} set DISPUTED from customer reply — automated follow-ups paused",
                "actor_label": "reply-triager", "level": "WARN",
            })
        elif category == "PAID_CLAIM":
            pod.table("invoices").update(inv["id"], {"status": "PAUSED", "notes": "Customer claims paid — verify (UTR 99812)"})
            pod.table("interactions").create({
                "invoice_id": inv["id"], "client_id": inv.get("client_id"),
                "kind": "STATUS_CHANGE", "channel": "SYSTEM", "direction": "INTERNAL",
                "summary": f"{no} paused — customer claims paid; awaiting reconciliation",
                "actor_label": "reply-triager", "level": "INFO",
            })

    return SeedRepliesResult(replies=n)
