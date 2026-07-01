#input_type_name: DispatchInput
#output_type_name: DispatchResult
#function_name: dispatch_followup

"""Send (or record) a follow-up for a draft using the mail mode configured in Settings:
  IN_APP - record only, no network (safe demo / "mock"); drafts are visible in the app
  GMAIL  - real delivery via the Lemma Gmail connector (delegated account, no creds here)
email_enabled is the master switch — off => IN_APP regardless. Updates the draft +
invoice and logs the attempt either way, so the pipeline works with zero setup.
"""

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


class DispatchInput(BaseModel):
    draft_id: str
    auto: bool = False
    edited_subject: Optional[str] = None
    edited_body: Optional[str] = None
    reviewer_note: Optional[str] = None


class DispatchResult(BaseModel):
    draft_id: str
    invoice_id: str
    status: str
    provider: str
    delivered: bool


def send_email(pod, cfg, to, subject, body):
    """Returns (provider, delivered, message_id, error). Lemma-native only:
    IN_APP records, GMAIL delivers through the Gmail connector (delegated)."""
    mode = (cfg.get("mail_mode") or "IN_APP").upper()
    if not cfg.get("email_enabled") or mode != "GMAIL" or not to:
        return ("in_app", True, None, None)
    try:
        res = pod.connectors.execute("workspace-gmail", "gmail_send_email",
            {"recipient_email": to, "subject": subject, "body": body}).to_dict().get("result", {})
        return ("gmail", True, str(res.get("id") or "") or None, None)
    except Exception as exc:
        return ("gmail", False, None, str(exc)[:400])


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

    cfg = (pod.records.list("pod_config", limit=1).to_dict()["items"] or [{}])[0]
    provider, delivered, mid, err = send_email(pod, cfg, to_email, subject, body)

    final_status = "FAILED" if not delivered else ("AUTO_SENT" if data.auto else "SENT")
    pod.table("drafts").update(data.draft_id, {
        "status": final_status, "subject": subject[:300], "body": body[:6000],
        "edited_by_human": edited, "reviewer_note": data.reviewer_note, "provider": provider,
        "provider_message_id": mid, "error_message": err,
        "sent_at": datetime.now(timezone.utc).isoformat() if delivered else None})

    if delivered and inv:
        pod.table("invoices").update(invoice_id, {
            "followup_count": int(inv.get("followup_count", 0)) + 1,
            "last_followup_at": datetime.now(timezone.utc).isoformat()})

    pod.table("interactions").create({
        "invoice_id": invoice_id, "client_id": inv.get("client_id") if inv else None, "draft_id": data.draft_id,
        "kind": "EMAIL_SENT" if delivered else "EMAIL_FAILED", "channel": draft.get("channel", "EMAIL"),
        "direction": "OUTBOUND",
        "summary": (f"{'Auto-sent' if data.auto else 'Approved & sent'} {inv.get('invoice_no') if inv else ''} via {provider}" + (" (edited)" if edited else "")) if delivered
                   else f"Send FAILED via {provider}: {err}",
        "detail": {"to": to_email, "provider": provider, "message_id": mid},
        "actor_label": "dispatch_followup", "level": "SUCCESS" if delivered else "ERROR"})

    return DispatchResult(draft_id=str(data.draft_id), invoice_id=invoice_id,
                          status=final_status, provider=provider, delivered=delivered)
