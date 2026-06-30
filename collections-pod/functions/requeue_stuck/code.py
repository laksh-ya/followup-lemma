#input_type_name: RequeueInput
#output_type_name: RequeueResult
#function_name: requeue_stuck

"""Self-healing for the collections pipeline (the dead-letter-retry equivalent).

A collections-run can fail on a transient LLM error (rate limit, or the model not
coercing to the strict draft schema under load), leaving its followup_queue row stuck
in QUEUED with no draft. This converges every such invoice:

  - if the invoice already produced a draft (auto-sent, sent, or PENDING_REVIEW at the
    approval form) → not stuck, leave it.
  - else if it has been retried fewer than MAX_RETRIES times → re-enqueue (re-triggers
    collections-run once the burst has passed).
  - else (LLM keeps failing) → write a DETERMINISTIC fallback draft (PENDING_REVIEW) so
    the invoice always ends with a reviewable draft, and resolve the queue row.

Run it on a short schedule for hands-off healing.
"""

from datetime import datetime, timezone

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod

MAX_RETRIES = 2

TONE_BY_STAGE = {"STAGE_1": "WARM_FRIENDLY", "STAGE_2": "POLITE_FIRM",
                 "STAGE_3": "FORMAL_SERIOUS", "STAGE_4": "STERN_URGENT"}


class RequeueInput(BaseModel):
    max_age_seconds: int = 90


class RequeueResult(BaseModel):
    retried: int
    fell_back: int
    stuck_invoice_ids: list[str]


def _age_seconds(iso: str) -> float:
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return 1e9


def _fallback_draft(pod, invoice_id, stage):
    inv = pod.table("invoices").get(invoice_id)
    if not inv:
        return
    client = pod.table("clients").get(str(inv["client_id"])) if inv.get("client_id") else {}
    cfg_items = pod.records.list("pod_config", limit=1).to_dict()["items"]
    sender = (cfg_items[0].get("sender_identity") if cfg_items else None) or "Finance team"
    st = stage if stage in TONE_BY_STAGE else "STAGE_2"
    name = (client or {}).get("name", "there")
    no = inv.get("invoice_no", "")
    cur = inv.get("currency", "INR")
    amt = float(inv["amount"])
    days = int(inv.get("days_overdue", 0))
    link = inv.get("payment_link") or "(contact finance for bank details)"
    subject = f"Reminder - Invoice {no} ({cur} {amt:,.2f}), {days} days overdue"
    body = (f"Dear {name},\n\nThis is a reminder regarding Invoice {no} for "
            f"{cur} {amt:,.2f}, which is now {days} days overdue. Please arrange "
            f"payment at your earliest convenience using the link below:\n{link}\n\n"
            f"If you have already paid, kindly share the payment reference so we can "
            f"reconcile our records.\n\nRegards,\n{sender}")
    # supersede older pending drafts
    existing = pod.records.list("drafts", filter=[
        {"field": "invoice_id", "op": "eq", "value": invoice_id},
        {"field": "status", "op": "eq", "value": "PENDING_REVIEW"},
    ]).to_dict()["items"]
    if existing:
        pod.records.bulk_update("drafts", [{"id": r["id"], "status": "SUPERSEDED"} for r in existing])
    draft = pod.table("drafts").create({
        "invoice_id": invoice_id, "client_id": inv.get("client_id"), "stage": st,
        "tone": TONE_BY_STAGE[st], "channel": "EMAIL", "subject": subject[:300], "body": body[:6000],
        "validation_passed": True, "validation_errors": [], "fallback_used": True,
        "status": "PENDING_REVIEW", "review_reason": "llm_unavailable_fallback",
        "model_used": "fallback/template",
    })
    pod.table("interactions").create({
        "invoice_id": invoice_id, "client_id": inv.get("client_id"), "draft_id": str(draft["id"]),
        "kind": "FALLBACK_USED", "channel": "SYSTEM", "direction": "INTERNAL",
        "summary": f"LLM unavailable after retries — deterministic fallback draft for {no}; routed to review",
        "actor_label": "requeue_stuck", "level": "WARN",
    })


async def requeue_stuck(ctx: FunctionContext, data: RequeueInput) -> RequeueResult:
    pod = Pod.from_env()
    rows = pod.records.list("followup_queue", limit=2000).to_dict()["items"]

    drafts = pod.records.list("drafts", limit=2000).to_dict()["items"]
    have_draft = {str(d["invoice_id"]) for d in drafts
                  if d.get("status") in ("PENDING_REVIEW", "APPROVED", "AUTO_SENT", "SENT")}

    # count prior retry attempts per invoice
    retry_counts = {}
    for r in rows:
        if r.get("reason") == "retry":
            retry_counts[str(r["invoice_id"])] = retry_counts.get(str(r["invoice_id"]), 0) + 1

    stuck = [r for r in rows
             if r.get("status") in ("QUEUED", "PROCESSING")
             and str(r.get("invoice_id")) not in have_draft
             and _age_seconds(str(r.get("created_at", ""))) >= data.max_age_seconds]

    retried_ids, fell_back = [], 0
    handled = set()
    for r in stuck:
        inv_id = str(r["invoice_id"])
        if inv_id in handled:
            continue
        handled.add(inv_id)
        if retry_counts.get(inv_id, 0) < MAX_RETRIES:
            pod.table("followup_queue").update(r["id"], {"status": "ERROR", "error": "orphaned; re-enqueued"})
            pod.table("followup_queue").create({"invoice_id": r["invoice_id"], "reason": "retry", "status": "QUEUED"})
            retried_ids.append(inv_id)
        else:
            inv = pod.table("invoices").get(inv_id)
            _fallback_draft(pod, inv_id, (inv or {}).get("stage", "STAGE_2"))
            pod.table("followup_queue").update(r["id"], {"status": "DONE", "error": "resolved via deterministic fallback"})
            fell_back += 1

    if retried_ids or fell_back:
        pod.table("interactions").create({
            "kind": "NOTE", "channel": "SYSTEM", "direction": "INTERNAL",
            "summary": f"Self-heal: {len(retried_ids)} re-enqueued, {fell_back} fell back to template",
            "detail": {"retried": retried_ids}, "actor_label": "requeue_stuck", "level": "INFO",
        })

    return RequeueResult(retried=len(retried_ids), fell_back=fell_back, stuck_invoice_ids=retried_ids)
