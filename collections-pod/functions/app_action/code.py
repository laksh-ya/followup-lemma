#input_type_name: AppActionInput
#output_type_name: AppActionResult
#function_name: app_action

"""Single write-entrypoint for the operator app. The browser invokes this for every
mutating action (scan, process an invoice, approve/edit/reject a draft, flag legal,
send a manual message, alert the team, save settings), so all business rules live
server-side and the UI stays thin.

Integrations are Lemma-native only: email goes out through the Gmail connector
(delegated), team/legal alerts through the Slack connector. IN_APP / no-channel modes
record the action without sending, so the pod demos itself with zero setup.
"""

from datetime import date, datetime, timezone
from typing import Optional

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


class AppActionInput(BaseModel):
    action: str   # scan | process | regenerate | approve | reject | flag | send_custom | notify | digest | save_config
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


def _send_email(pod, cfg, to, subject, body):
    """(provider, delivered, message_id, error). Lemma-native: IN_APP records only,
    GMAIL delivers via the Gmail connector on the invoking user's account."""
    mode = (cfg.get("mail_mode") or "IN_APP").upper()
    if not cfg.get("email_enabled") or mode != "GMAIL" or not to:
        return ("in_app", True, None, None)
    try:
        res = pod.connectors.execute("workspace-gmail", "gmail_send_email",
            {"recipient_email": to, "subject": subject, "body": body}).to_dict().get("result", {})
        return ("gmail", True, str(res.get("id") or "") or None, None)
    except Exception as exc:
        return ("gmail", False, None, str(exc)[:400])


def _notify(pod, cfg, audience, message):
    """(delivered, info). Lemma-native Slack connector; records when no channel set."""
    channel = (cfg.get("notify_channel") or "NONE").upper()
    if channel != "SLACK":
        return (False, "no channel configured (record-only)")
    target = (cfg.get("slack_legal_channel") if audience == "legal" else cfg.get("slack_channel")) or cfg.get("slack_channel") or ""
    if not target:
        return (False, "Slack channel id not set in Settings")
    try:
        pod.connectors.execute("workspace-slack", "chat_post_message", {"channel": target, "text": message})
        return (True, "sent via Slack")
    except Exception as exc:
        return (False, "connect the Slack account first: " + str(exc)[:160])


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
        dr = pod.records.list("drafts", limit=2000).to_dict()["items"]
        drafted = {str(d["invoice_id"]) for d in dr if d.get("status") in ("PENDING_REVIEW", "APPROVED", "AUTO_SENT", "SENT")}
        n = 0
        for inv in actives:
            try:
                delta = (date.today() - date.fromisoformat(str(inv.get("due_date"))[:10])).days
            except Exception:
                continue
            iid = str(inv["id"])
            if delta > 0 and iid not in queued and iid not in drafted:
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
            to_email = (client or {}).get("email", "")
            provider, delivered, mid, err = _send_email(pod, cfg, to_email, subject, body)
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

    if a == "send_custom":
        # manual email to a customer (by invoice_id or by client_id in config)
        cfg = (pod.records.list("pod_config", limit=1).to_dict()["items"] or [{}])[0]
        fields = data.config or {}
        inv = pod.table("invoices").get(data.invoice_id) if data.invoice_id else None
        client = None
        if inv and inv.get("client_id"):
            client = pod.table("clients").get(str(inv["client_id"]))
        elif fields.get("client_id"):
            client = pod.table("clients").get(str(fields["client_id"]))
        to = (client or {}).get("email", "") if client else fields.get("to", "")
        subject = fields.get("subject") or data.edited_subject or "A note about your account"
        body = data.edited_body or fields.get("body") or ""
        provider, delivered, mid, err = _send_email(pod, cfg, to, subject, body)
        _log(pod, invoice_id=data.invoice_id, client_id=(client or {}).get("id") if client else None,
             kind="EMAIL_SENT" if delivered else "EMAIL_FAILED", channel="EMAIL", direction="OUTBOUND",
             summary=(f"Manual message to {(client or {}).get('name', to)} via {provider}") if delivered else f"Manual send FAILED via {provider}: {err}",
             detail={"to": to, "subject": subject, "manual": True}, actor_user=ctx.user_id, actor_label="operator",
             level="SUCCESS" if delivered else "ERROR")
        return AppActionResult(ok=delivered, action=a, detail=f"{'sent' if delivered else 'recorded'} via {provider}")

    if a == "notify":
        cfg = (pod.records.list("pod_config", limit=1).to_dict()["items"] or [{}])[0]
        audience = data.note or "team"
        msg = (data.config or {}).get("message") or "Update from Collections Agent"
        delivered, info = _notify(pod, cfg, audience, msg)
        _log(pod, kind="NOTIFICATION_SENT", channel="SLACK" if delivered else "SYSTEM", direction="OUTBOUND",
             summary=f"[{audience}] {msg[:180]}", detail={"delivered": delivered, "info": info},
             actor_user=ctx.user_id, actor_label="app", level="SUCCESS" if delivered else "INFO")
        return AppActionResult(ok=True, action=a, detail=info)

    if a == "digest":
        invs = pod.records.list("invoices", limit=2000).to_dict()["items"]
        active = [i for i in invs if i.get("status") == "ACTIVE"]
        outstanding = sum(float(i.get("amount") or 0) for i in active)
        overdue = sum(1 for i in active if int(i.get("days_overdue") or 0) > 0)
        legal = sum(1 for i in invs if i.get("status") == "LEGAL")
        pend = len(pod.records.list("drafts", limit=2000, filter=[{"field": "status", "op": "eq", "value": "PENDING_REVIEW"}]).to_dict()["items"])
        msg = (f"Collections digest\nOutstanding: {outstanding:,.0f}\nOverdue accounts: {overdue}\n"
               f"Awaiting approval: {pend}\nIn legal: {legal}")
        cfg = (pod.records.list("pod_config", limit=1).to_dict()["items"] or [{}])[0]
        delivered, info = _notify(pod, cfg, "team", msg)
        _log(pod, kind="NOTIFICATION_SENT", channel="SLACK" if delivered else "SYSTEM", direction="OUTBOUND",
             summary="Daily digest sent to team" if delivered else "Daily digest (recorded)",
             detail={"message": msg, "delivered": delivered}, actor_user=ctx.user_id, actor_label="app",
             level="SUCCESS" if delivered else "INFO")
        return AppActionResult(ok=True, action=a, detail=("sent to team" if delivered else "digest recorded (" + info + ")"))

    if a == "change_status":
        if not data.invoice_id:
            return AppActionResult(ok=False, action=a, detail="invoice_id required")
        new_status = (data.config or {}).get("status")
        if not new_status:
            return AppActionResult(ok=False, action=a, detail="status required in config")
        inv = pod.table("invoices").get(data.invoice_id)
        if not inv:
            return AppActionResult(ok=False, action=a, detail="invoice not found")
        pod.table("invoices").update(data.invoice_id, {"status": new_status})
        _log(pod, invoice_id=data.invoice_id, client_id=inv.get("client_id"),
             kind="STATUS_CHANGED", channel="SYSTEM", direction="INTERNAL",
             summary=f"Operator changed status of {inv.get('invoice_no', '')} to {new_status}",
             actor_user=ctx.user_id, actor_label="operator", level="INFO")
        return AppActionResult(ok=True, action=a, detail=f"status changed to {new_status}")

    if a == "save_config":
        rows = pod.records.list("pod_config", limit=1).to_dict()["items"]
        fields = data.config or {}
        allowed = {"auto_dispatch", "human_in_loop", "review_stage4", "review_high_risk",
                   "email_enabled", "mail_mode", "email_from",
                   "notify_channel", "slack_channel", "slack_legal_channel",
                   "company_name", "sender_identity"}
        patch = {k: v for k, v in fields.items() if k in allowed}
        if rows:
            pod.table("pod_config").update(rows[0]["id"], patch)
        else:
            pod.table("pod_config").create({"singleton": "main", **patch})
        return AppActionResult(ok=True, action=a, detail="settings saved")

    return AppActionResult(ok=False, action=a, detail=f"unknown action {a}")
