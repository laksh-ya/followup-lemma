#input_type_name: AppActionInput
#output_type_name: AppActionResult
#function_name: app_action

"""Single write-entrypoint for the operator app. The browser invokes this for every
mutating action (scan, process an invoice, approve/edit/reject a draft, flag legal,
save settings), so all business rules live server-side and the UI stays thin.
"""

from datetime import date, datetime, timezone
from typing import Optional

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


class AppActionInput(BaseModel):
    action: str                       # scan | process | approve | reject | flag | save_config
    invoice_id: Optional[str] = None
    draft_id: Optional[str] = None
    edited_subject: Optional[str] = None
    edited_body: Optional[str] = None
    note: Optional[str] = None
    config: Optional[dict] = None


class AppActionResult(BaseModel):
    ok: bool
    action: str
    detail: str


def _log(pod, **kw):
    pod.table("interactions").create(kw)


def _latest_pending_draft(pod, invoice_id):
    rows = pod.records.list("drafts", limit=50, filter=[
        {"field": "invoice_id", "op": "eq", "value": invoice_id},
        {"field": "status", "op": "eq", "value": "PENDING_REVIEW"},
    ], sort=[{"field": "created_at", "direction": "desc"}]).to_dict()["items"]
    return rows[0] if rows else None


async def app_action(ctx: FunctionContext, data: AppActionInput) -> AppActionResult:
    pod = Pod.from_env()
    a = data.action

    if a == "scan":
        actives = pod.records.list("invoices", limit=1000, filter=[
            {"field": "status", "op": "eq", "value": "ACTIVE"}]).to_dict()["items"]
        q = pod.records.list("followup_queue", limit=1000).to_dict()["items"]
        queued = {str(r["invoice_id"]) for r in q if r.get("status") in ("QUEUED", "PROCESSING")}
        n = 0
        for inv in actives:
            try:
                delta = (date.today() - date.fromisoformat(str(inv.get("due_date"))[:10])).days
            except Exception:
                continue
            if delta > 0 and str(inv["id"]) not in queued:
                pod.table("followup_queue").create({"invoice_id": inv["id"], "reason": "manual", "status": "QUEUED"})
                n += 1
        _log(pod, kind="NOTE", channel="SYSTEM", direction="INTERNAL",
             summary=f"Operator ran scan — {n} invoice(s) enqueued", actor_label="app", level="INFO")
        return AppActionResult(ok=True, action=a, detail=f"enqueued {n}")

    if a in ("process", "regenerate"):
        if not data.invoice_id:
            return AppActionResult(ok=False, action=a, detail="invoice_id required")
        pod.table("followup_queue").create({"invoice_id": data.invoice_id, "reason": "manual", "status": "QUEUED"})
        return AppActionResult(ok=True, action=a, detail="enqueued for processing")

    if a in ("approve", "reject", "flag"):
        draft = pod.table("drafts").get(data.draft_id) if data.draft_id else _latest_pending_draft(pod, data.invoice_id)
        if not draft:
            return AppActionResult(ok=False, action=a, detail="no pending draft for invoice")
        invoice_id = str(draft["invoice_id"])
        inv = pod.table("invoices").get(invoice_id)
        client = pod.table("clients").get(str(inv["client_id"])) if inv and inv.get("client_id") else {}

        if a == "approve":
            subject = data.edited_subject or draft.get("subject")
            body = data.edited_body or draft.get("body")
            edited = bool(data.edited_subject or data.edited_body)
            cfg = (pod.records.list("pod_config", limit=1).to_dict()["items"] or [{}])[0]
            email_enabled = bool(cfg.get("email_enabled", False))
            provider, delivered, err, mid = "in_app", True, None, None
            to_email = (client or {}).get("email", "")
            if email_enabled and to_email:
                try:
                    res = pod.connectors.execute("workspace-gmail", "gmail_send_email",
                        {"recipient_email": to_email, "subject": subject, "body": body}).to_dict().get("result", {})
                    provider, mid = "gmail", str(res.get("id") or "") or None
                except Exception as exc:
                    provider, delivered, err = "gmail", False, str(exc)[:800]
            pod.table("drafts").update(draft["id"], {
                "status": "SENT" if delivered else "FAILED", "subject": subject[:300], "body": body[:6000],
                "edited_by_human": edited, "reviewer_note": data.note, "provider": provider,
                "provider_message_id": mid, "error_message": err,
                "sent_at": datetime.now(timezone.utc).isoformat() if delivered else None})
            if delivered and inv:
                pod.table("invoices").update(invoice_id, {
                    "followup_count": int(inv.get("followup_count", 0)) + 1,
                    "last_followup_at": datetime.now(timezone.utc).isoformat()})
            _log(pod, invoice_id=invoice_id, client_id=inv.get("client_id") if inv else None, draft_id=str(draft["id"]),
                 kind="EMAIL_SENT" if delivered else "EMAIL_FAILED", channel=draft.get("channel", "EMAIL"),
                 direction="OUTBOUND",
                 summary=(f"Approved & sent {inv.get('invoice_no') if inv else ''} via {provider}" + (" (edited)" if edited else "")) if delivered else f"Approved send FAILED via {provider}",
                 actor_user=ctx.user_id, actor_label="reviewer", level="SUCCESS" if delivered else "ERROR")
            return AppActionResult(ok=delivered, action=a, detail=f"{'sent' if delivered else 'failed'} via {provider}")

        if a == "reject":
            pod.table("drafts").update(draft["id"], {"status": "REJECTED", "reviewer_note": data.note})
            _log(pod, invoice_id=invoice_id, client_id=inv.get("client_id") if inv else None, draft_id=str(draft["id"]),
                 kind="HUMAN_REJECTED", channel="SYSTEM", direction="INTERNAL",
                 summary="Reviewer rejected the draft; no message sent" + (f" — {data.note}" if data.note else ""),
                 actor_user=ctx.user_id, actor_label="reviewer", level="WARN")
            return AppActionResult(ok=True, action=a, detail="rejected")

        # flag legal
        pod.table("invoices").update(invoice_id, {"status": "LEGAL"})
        pod.table("drafts").update(draft["id"], {"status": "REJECTED", "reviewer_note": data.note or "flagged legal"})
        _log(pod, invoice_id=invoice_id, client_id=inv.get("client_id") if inv else None,
             kind="LEGAL_FLAGGED", channel="SYSTEM", direction="INTERNAL",
             summary=f"{inv.get('invoice_no') if inv else ''} flagged LEGAL by reviewer" + (f" — {data.note}" if data.note else ""),
             actor_user=ctx.user_id, actor_label="reviewer", level="ERROR")
        return AppActionResult(ok=True, action=a, detail="flagged legal")

    if a == "save_config":
        rows = pod.records.list("pod_config", limit=1).to_dict()["items"]
        fields = data.config or {}
        allowed = {"auto_dispatch", "human_in_loop", "review_stage4", "review_high_risk",
                   "email_enabled", "notify_channel", "company_name", "sender_identity"}
        patch = {k: v for k, v in fields.items() if k in allowed}
        if rows:
            pod.table("pod_config").update(rows[0]["id"], patch)
        else:
            pod.table("pod_config").create({"singleton": "main", **patch})
        return AppActionResult(ok=True, action=a, detail="settings saved")

    return AppActionResult(ok=False, action=a, detail=f"unknown action {a}")
