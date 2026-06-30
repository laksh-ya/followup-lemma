#input_type_name: MetricsInput
#output_type_name: MetricsResult
#function_name: metrics_snapshot

"""Aggregate dashboard metrics in one call: totals, stage distribution, risk
distribution, and pending review / legal / failure counts. Read-only.
"""

from datetime import date

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


class MetricsInput(BaseModel):
    pass


class MetricsResult(BaseModel):
    totals: dict
    stage_counts: dict
    risk_counts: dict
    status_counts: dict


async def metrics_snapshot(ctx: FunctionContext, data: MetricsInput) -> MetricsResult:
    pod = Pod.from_env()

    def rows(sql):
        return pod.query(sql).to_dict()["items"]

    stage_counts = {r["stage"]: r["n"] for r in rows(
        "select stage, count(*) as n from invoices group by stage")}
    risk_counts = {r["risk_level"]: r["n"] for r in rows(
        "select risk_level, count(*) as n from invoices group by risk_level")}
    status_counts = {r["status"]: r["n"] for r in rows(
        "select status, count(*) as n from invoices group by status")}

    total = sum(status_counts.values())
    overdue = sum(stage_counts.get(s, 0) for s in ("STAGE_1", "STAGE_2", "STAGE_3", "STAGE_4", "ESCALATED"))
    paid = status_counts.get("PAID", 0)
    legal = status_counts.get("LEGAL", 0)

    pending_review = rows("select count(*) as n from drafts where status = 'PENDING_REVIEW'")[0]["n"]
    failed = rows("select count(*) as n from drafts where status = 'FAILED'")[0]["n"]
    queue_errors = rows("select count(*) as n from followup_queue where status = 'ERROR'")[0]["n"]

    today = date.today().isoformat()
    sent_today = rows(
        "select count(*) as n from drafts where status in ('SENT','AUTO_SENT') "
        f"and substr(cast(sent_at as text),1,10) = '{today}'")[0]["n"]

    outstanding = rows(
        "select coalesce(sum(amount),0) as s from invoices where status = 'ACTIVE'")[0]["s"]

    return MetricsResult(
        totals={
            "invoices": total,
            "overdue": overdue,
            "paid": paid,
            "legal": legal,
            "pending_review": pending_review,
            "sent_today": sent_today,
            "failed": failed,
            "queue_errors": queue_errors,
            "outstanding_amount": outstanding,
        },
        stage_counts=stage_counts,
        risk_counts=risk_counts,
        status_counts=status_counts,
    )
