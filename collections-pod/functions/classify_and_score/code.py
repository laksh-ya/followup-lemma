#input_type_name: ClassifyInput
#output_type_name: ClassifyResult
#function_name: classify_and_score

"""Recompute days overdue, assign escalation stage, compute a deterministic risk
level, decide whether the draft will need human review, and persist it all on the
invoice. This is the deterministic front of the collections pipeline — no LLM.
"""

from datetime import date, datetime, timezone

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


class ClassifyInput(BaseModel):
    invoice_id: str


class ClassifyResult(BaseModel):
    invoice_id: str
    invoice_no: str
    client_id: str
    client_name: str
    client_email: str
    amount: float
    currency: str
    due_date: str
    payment_link: str
    vip: bool
    payment_behavior: str
    client_notes: str
    days_overdue: int
    stage: str
    status: str
    risk_level: str
    is_escalated: bool
    needs_review: bool
    review_reason: str


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


def _risk_for(amount: float, days: int, payment_behavior: str, vip: bool) -> str:
    amount_norm = min(max(amount, 0.0) / 200000.0, 1.0)
    days_norm = min(max(days, 0) / 30.0, 1.0)
    behavior_weight = {"GOOD": 0.0, "AVERAGE": 0.5, "RISKY": 1.0}.get(payment_behavior, 0.5)
    vip_bump = 0.1 if vip else 0.0
    score = 0.4 * amount_norm + 0.4 * days_norm + 0.2 * behavior_weight + vip_bump
    if score >= 0.66:
        return "HIGH"
    if score >= 0.33:
        return "MEDIUM"
    return "LOW"


def _parse_date(value) -> date:
    s = str(value)
    # tolerate full datetimes too
    return date.fromisoformat(s[:10])


async def classify_and_score(ctx: FunctionContext, data: ClassifyInput) -> ClassifyResult:
    pod = Pod.from_env()
    inv = pod.table("invoices").get(data.invoice_id)
    if not inv:
        raise ValueError(f"invoice not found: {data.invoice_id}")

    client = pod.table("clients").get(str(inv["client_id"]))
    payment_behavior = (client or {}).get("payment_behavior", "AVERAGE")
    vip = bool((client or {}).get("vip", False))
    client_name = (client or {}).get("name", "the client")

    delta = (date.today() - _parse_date(inv["due_date"])).days
    days_overdue = max(0, delta)
    stage = _stage_for_days(delta)
    risk_level = _risk_for(float(inv["amount"]), days_overdue, payment_behavior, vip)

    # Runtime config gates
    cfg_items = pod.records.list("pod_config", limit=1).to_dict()["items"]
    cfg = cfg_items[0] if cfg_items else {}
    auto_dispatch = bool(cfg.get("auto_dispatch", True))
    human_in_loop = bool(cfg.get("human_in_loop", True))
    review_stage4 = bool(cfg.get("review_stage4", True))
    review_high_risk = bool(cfg.get("review_high_risk", True))

    is_escalated = stage == "ESCALATED"

    needs_review = False
    review_reason = "low_risk_auto"
    if human_in_loop:
        if review_high_risk and risk_level == "HIGH":
            needs_review, review_reason = True, "high_risk"
        elif review_stage4 and stage == "STAGE_4":
            needs_review, review_reason = True, "stage_4"
    if not auto_dispatch:
        needs_review = True
        if review_reason == "low_risk_auto":
            review_reason = "auto_dispatch_off"

    pod.table("invoices").update(
        data.invoice_id,
        {
            "days_overdue": days_overdue,
            "stage": stage,
            "risk_level": risk_level,
            "last_processed_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    return ClassifyResult(
        invoice_id=str(data.invoice_id),
        invoice_no=str(inv.get("invoice_no", "")),
        client_id=str(inv["client_id"]),
        client_name=str(client_name),
        client_email=str((client or {}).get("email", "")),
        amount=float(inv["amount"]),
        currency=str(inv.get("currency", "INR")),
        due_date=str(inv.get("due_date", ""))[:10],
        payment_link=str(inv.get("payment_link") or ""),
        vip=vip,
        payment_behavior=str(payment_behavior),
        client_notes=str((client or {}).get("notes") or ""),
        days_overdue=days_overdue,
        stage=stage,
        status=str(inv.get("status", "ACTIVE")),
        risk_level=risk_level,
        is_escalated=is_escalated,
        needs_review=needs_review,
        review_reason=review_reason,
    )
