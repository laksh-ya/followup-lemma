#input_type_name: FallbackInput
#output_type_name: FallbackResult
#function_name: make_fallback_draft

"""Deterministic per-stage follow-up template, used when the LLM draft fails
validation. Always persisted PENDING_REVIEW (a human checks any fallback) so we never
auto-send a non-LLM template silently. Ported from the prior app's mock generator.
"""

from typing import Optional

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod

TONE_BY_STAGE = {
    "STAGE_1": "WARM_FRIENDLY",
    "STAGE_2": "POLITE_FIRM",
    "STAGE_3": "FORMAL_SERIOUS",
    "STAGE_4": "STERN_URGENT",
}

SUBJECT_BY_STAGE = {
    "STAGE_1": "Quick reminder - Invoice {no} ({cur} {amt:,.2f})",
    "STAGE_2": "Follow-up: Invoice {no} is {days} days overdue",
    "STAGE_3": "Important: Outstanding payment - Invoice {no} ({days} days overdue)",
    "STAGE_4": "FINAL NOTICE - Invoice {no} - Immediate action required",
}

BODY_BY_STAGE = {
    "STAGE_1": (
        "Hi {name},\n\nA friendly reminder that Invoice {no} for {cur} {amt:,.2f} "
        "(due {due}) is now {days} day(s) overdue. If you've already paid, please "
        "disregard. Otherwise you can settle it here: {link}\n\nThank you!\n{sender}"
    ),
    "STAGE_2": (
        "Hi {name},\n\nFollowing up on Invoice {no} for {cur} {amt:,.2f}, due {due} "
        "and now {days} days overdue. Could you confirm the expected payment date? "
        "You can pay via: {link}\n\nThanks for your prompt attention.\n{sender}"
    ),
    "STAGE_3": (
        "Dear {name},\n\nDespite earlier reminders, Invoice {no} for {cur} {amt:,.2f} "
        "(due {due}) remains unpaid and is now {days} days overdue. Continued "
        "non-payment may affect your credit terms with us. Please respond within 48 "
        "hours with payment confirmation or a resolution plan. Payment link: {link}\n\n"
        "Regards,\n{sender}"
    ),
    "STAGE_4": (
        "Dear {name},\n\nThis is our final automated reminder regarding Invoice {no} "
        "for {cur} {amt:,.2f}, now {days} days overdue (due {due}). Failure to remit "
        "payment within 24 hours will result in escalation to our finance and legal "
        "team. Please act immediately using the payment link: {link}\n\nRegards,\n{sender}"
    ),
}


class FallbackInput(BaseModel):
    invoice_id: str
    stage: str
    reason: str = "validation_failed"
    risk_level: Optional[str] = None
    channel: str = "EMAIL"


class FallbackResult(BaseModel):
    draft_id: str
    stage: str


async def make_fallback_draft(ctx: FunctionContext, data: FallbackInput) -> FallbackResult:
    pod = Pod.from_env()
    inv = pod.table("invoices").get(data.invoice_id)
    if not inv:
        raise ValueError(f"invoice not found: {data.invoice_id}")
    client = pod.table("clients").get(str(inv["client_id"])) if inv.get("client_id") else {}

    stage = data.stage if data.stage in TONE_BY_STAGE else "STAGE_2"
    cfg_items = pod.records.list("pod_config", limit=1).to_dict()["items"]
    sender = (cfg_items[0].get("sender_identity") if cfg_items else None) or "Finance team"

    fmt = {
        "name": (client or {}).get("name", "there"),
        "no": inv.get("invoice_no", ""),
        "cur": inv.get("currency", "INR"),
        "amt": float(inv["amount"]),
        "due": str(inv.get("due_date", ""))[:10],
        "days": int(inv.get("days_overdue", 0)),
        "link": inv.get("payment_link") or "(contact finance for bank details)",
        "sender": sender,
    }
    subject = SUBJECT_BY_STAGE[stage].format(**fmt)
    body = BODY_BY_STAGE[stage].format(**fmt)

    # supersede any prior active draft for this invoice
    prior = pod.records.list("drafts", limit=200, filter=[
        {"field": "invoice_id", "op": "eq", "value": data.invoice_id},
    ]).to_dict()["items"]
    to_sup = [r["id"] for r in prior if r.get("status") in ("PENDING_REVIEW", "APPROVED", "AUTO_SENT", "SENT", "FAILED")]
    if to_sup:
        pod.records.bulk_update("drafts", [{"id": i, "status": "SUPERSEDED"} for i in to_sup])

    draft = pod.table("drafts").create({
        "invoice_id": data.invoice_id,
        "client_id": inv.get("client_id"),
        "stage": stage,
        "tone": TONE_BY_STAGE[stage],
        "channel": data.channel,
        "subject": subject[:300],
        "body": body[:6000],
        "confidence": None,
        "risk_level": data.risk_level,
        "validation_passed": True,
        "validation_errors": [],
        "fallback_used": True,
        "status": "PENDING_REVIEW",
        "review_reason": data.reason or "fallback_used",
        "model_used": "fallback/template",
    })
    draft_id = str(draft["id"])

    pod.table("interactions").create({
        "invoice_id": data.invoice_id,
        "client_id": inv.get("client_id"),
        "draft_id": draft_id,
        "kind": "FALLBACK_USED",
        "channel": "SYSTEM",
        "direction": "INTERNAL",
        "summary": f"Deterministic fallback draft created for {inv.get('invoice_no')} ({stage}); routed to human review",
        "detail": {"reason": data.reason},
        "actor_label": "make_fallback_draft",
        "level": "WARN",
    })

    return FallbackResult(draft_id=draft_id, stage=stage)
