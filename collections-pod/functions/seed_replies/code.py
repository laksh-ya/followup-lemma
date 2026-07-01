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
    
    import random
    active_invs = [i for i in invs if i.get("status") == "ACTIVE"]
    if not active_invs:
        active_invs = invs
    
    if not active_invs:
        return SeedRepliesResult(replies=0)

    # Let's shuffle and take up to 8 invoices
    rng = random.Random(42)
    selected_invs = list(active_invs)
    rng.shuffle(selected_invs)
    selected_invs = selected_invs[:8]

    REPLY_TEMPLATES = [
        ("PROMISE_TO_PAY", "Apologies for the delay — we'll clear this by Friday.", 5),
        ("PAID_CLAIM", "We've already paid this last week, please check UTR ref: 99812.", None),
        ("DISPUTE", "We dispute two line items on this invoice; details attached.", None),
        ("QUESTION", "Can you resend the payment link? The old one expired.", None),
        ("PROMISE_TO_PAY", "Our accounting team is processing this. Expected payment within 3 days.", 3),
        ("PAID_CLAIM", "Payment has been processed, please check your bank statement.", None),
        ("DISPUTE", "We never received these items. Please verify delivery.", None),
        ("QUESTION", "Who is the billing manager? I need to ask a question.", None),
    ]

    n = 0
    for idx, inv in enumerate(selected_invs):
        template = REPLY_TEMPLATES[idx % len(REPLY_TEMPLATES)]
        category, body, promise_days = template
        no = inv.get("invoice_no", "")
        client = pod.table("clients").get(str(inv["client_id"])) if inv.get("client_id") else {}
        frm = (client or {}).get("email", "")
        
        # Check if we already have replies for this invoice to prevent duplication
        # (For idempotency we can check interactions of type REPLY_RECEIVED)
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
            pod.table("invoices").update(inv["id"], {"status": "PAUSED", "notes": f"Customer claims paid — verify (Ref: {no})"})
            pod.table("interactions").create({
                "invoice_id": inv["id"], "client_id": inv.get("client_id"),
                "kind": "STATUS_CHANGE", "channel": "SYSTEM", "direction": "INTERNAL",
                "summary": f"{no} paused — customer claims paid; awaiting reconciliation",
                "actor_label": "reply-triager", "level": "INFO",
            })

    return SeedRepliesResult(replies=n)
