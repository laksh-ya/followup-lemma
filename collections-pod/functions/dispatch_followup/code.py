#input_type_name: DispatchInput
#output_type_name: DispatchResult
#function_name: dispatch_followup

"""Send (or record) a follow-up for a draft. If email_enabled is on and a Gmail
connector account is available, send for real; otherwise record an in-app send so the
whole pipeline works with zero connectors. Updates the draft + invoice and logs an
interaction on every attempt (success or failure).
"""

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


class DispatchInput(BaseModel):
    draft_id: str
    auto: bool = False                 # True = auto-sent (no human); False = human-approved
    edited_subject: Optional[str] = None
    edited_body: Optional[str] = None
    reviewer_note: Optional[str] = None


class DispatchResult(BaseModel):
    draft_id: str
    invoice_id: str
    status: str        # AUTO_SENT | SENT | FAILED
    provider: str      # gmail | in_app
    delivered: bool


async def dispatch_followup(ctx: FunctionContext, data: DispatchInput) -> DispatchResult:
    pod = Pod.from_env()
    draft = pod.table("drafts").get(data.draft_id)
    if not draft:
        raise ValueError(f"draft not found: {data.draft_id}")
    invoice_id = str(draft["invoice_id"])
    inv = pod.table("invoices").get(invoice_id)
    client = pod.table("clients").get(str(inv["client_id"])) if inv and inv.get("client_id") else {}
    to_email = (client or {}).get("email", "")

    subject = data.edited_subject or draft.get("subject") or ""
    body = data.edited_body or draft.get("body") or ""
    edited = bool(data.edited_subject or data.edited_body)

    cfg_items = pod.records.list("pod_config", limit=1).to_dict()["items"]
    cfg = cfg_items[0] if cfg_items else {}
    email_enabled = bool(cfg.get("email_enabled", False))

    provider = "in_app"
    delivered = True
    error_message = None
    provider_message_id = None

    if email_enabled and to_email:
        try:
            res = pod.connectors.execute(
                "workspace-gmail",
                "gmail_send_email",
                {"recipient_email": to_email, "subject": subject, "body": body},
            ).to_dict().get("result", {})
            provider = "gmail"
            provider_message_id = str(res.get("id") or res.get("message_id") or "") or None
        except Exception as exc:  # connector missing/misconfigured/transient
            provider = "gmail"
            delivered = False
            error_message = str(exc)[:900]

    final_status = "FAILED" if not delivered else ("AUTO_SENT" if data.auto else "SENT")

    pod.table("drafts").update(data.draft_id, {
        "status": final_status,
        "subject": subject[:300],
        "body": body[:6000],
        "edited_by_human": edited,
        "reviewer_note": data.reviewer_note,
        "provider": provider,
        "provider_message_id": provider_message_id,
        "error_message": error_message,
        "sent_at": datetime.now(timezone.utc).isoformat() if delivered else None,
    })

    if delivered and inv:
        pod.table("invoices").update(invoice_id, {
            "followup_count": int(inv.get("followup_count", 0)) + 1,
            "last_followup_at": datetime.now(timezone.utc).isoformat(),
        })

    pod.table("interactions").create({
        "invoice_id": invoice_id,
        "client_id": inv.get("client_id") if inv else None,
        "draft_id": data.draft_id,
        "kind": "EMAIL_SENT" if delivered else "EMAIL_FAILED",
        "channel": draft.get("channel", "EMAIL"),
        "direction": "OUTBOUND",
        "summary": (
            f"{'Auto-sent' if data.auto else 'Approved & sent'} {inv.get('invoice_no') if inv else ''} "
            f"via {provider}" + (" (edited)" if edited else "") if delivered
            else f"Send FAILED via {provider}: {error_message}"
        ),
        "detail": {"to": to_email, "provider": provider, "message_id": provider_message_id},
        "actor_label": "dispatch_followup",
        "level": "SUCCESS" if delivered else "ERROR",
    })

    return DispatchResult(
        draft_id=str(data.draft_id), invoice_id=invoice_id,
        status=final_status, provider=provider, delivered=delivered,
    )
