#input_type_name: ScanInput
#output_type_name: ScanResult
#function_name: scan_overdue

"""Find ACTIVE overdue invoices and enqueue them for follow-up. Refreshes
days_overdue/stage on each ACTIVE invoice so the dashboard is always current, then
inserts a followup_queue row per invoice that needs work. A DATASTORE schedule on
followup_queue starts collections-run for each enqueued row (reactive fan-out).
"""

from datetime import date, datetime, timezone

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


class ScanInput(BaseModel):
    force: bool = False          # reprocess even if already processed today
    failed_only: bool = False    # only re-queue invoices whose last send FAILED
    reason: str = "overdue_scan"


class ScanResult(BaseModel):
    enqueued: int
    skipped: int
    refreshed: int


def _stage_for_days(days: int) -> str:
    if days <= 0:
        return "PENDING"
    if days <= 7:
        return "STAGE_1"
    if days <= 14:
        return "STAGE_2"
    if days <= 21:
        return "STAGE_3"
    if days <= 30:
        return "STAGE_4"
    return "ESCALATED"


def _today_iso() -> str:
    return date.today().isoformat()


async def scan_overdue(ctx: FunctionContext, data: ScanInput) -> ScanResult:
    pod = Pod.from_env()

    actives = pod.records.list("invoices", limit=1000, filter=[
        {"field": "status", "op": "eq", "value": "ACTIVE"},
    ]).to_dict()["items"]

    # invoices already sitting in the queue (don't double-enqueue)
    all_queue = pod.records.list("followup_queue", limit=1000).to_dict()["items"]
    queued_ids = {str(q["invoice_id"]) for q in all_queue if q.get("status") in ("QUEUED", "PROCESSING")}

    failed_ids = set()
    if data.failed_only:
        failed = pod.records.list("drafts", limit=1000, filter=[
            {"field": "status", "op": "eq", "value": "FAILED"},
        ]).to_dict()["items"]
        failed_ids = {str(d["invoice_id"]) for d in failed}

    # invoices that already have a current draft — don't re-draft on an automatic scan
    alldrafts = pod.records.list("drafts", limit=2000).to_dict()["items"]
    active_draft_ids = {str(d["invoice_id"]) for d in alldrafts
                        if d.get("status") in ("PENDING_REVIEW", "APPROVED", "AUTO_SENT", "SENT")}

    enqueued = skipped = refreshed = 0
    to_enqueue = []
    refresh_updates = []

    for inv in actives:
        due = str(inv.get("due_date", ""))[:10]
        try:
            delta = (date.today() - date.fromisoformat(due)).days
        except Exception:
            skipped += 1
            continue
        days_overdue = max(0, delta)
        stage = _stage_for_days(delta)

        # keep the invoice's derived fields fresh for the UI
        if inv.get("days_overdue") != days_overdue or inv.get("stage") != stage:
            refresh_updates.append({"id": inv["id"], "days_overdue": days_overdue, "stage": stage})
            refreshed += 1

        if delta <= 0:
            skipped += 1
            continue
        if data.failed_only and str(inv["id"]) not in failed_ids:
            skipped += 1
            continue
        if str(inv["id"]) in queued_ids:
            skipped += 1
            continue
        if not data.force and str(inv["id"]) in active_draft_ids:
            skipped += 1
            continue
        if not data.force and not data.failed_only:
            lp = inv.get("last_processed_at")
            if lp and str(lp)[:10] == _today_iso():
                skipped += 1
                continue
        to_enqueue.append(inv)

    if refresh_updates:
        pod.records.bulk_update("invoices", refresh_updates)

    for inv in to_enqueue:
        pod.table("followup_queue").create({
            "invoice_id": inv["id"],
            "reason": "resend_failed" if data.failed_only else data.reason,
            "status": "QUEUED",
        })
        enqueued += 1

    pod.table("interactions").create({
        "kind": "NOTE",
        "channel": "SYSTEM",
        "direction": "INTERNAL",
        "summary": f"Scan complete — {enqueued} enqueued, {skipped} skipped, {refreshed} refreshed",
        "detail": {"force": data.force, "failed_only": data.failed_only},
        "actor_label": "scan_overdue",
        "level": "INFO",
    })

    return ScanResult(enqueued=enqueued, skipped=skipped, refreshed=refreshed)
