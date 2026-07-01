#input_type_name: ValidateInput
#output_type_name: ValidateResult
#function_name: validate_draft

"""Validate a generated follow-up against the source invoice and persist it as a
draft. This is the anti-hallucination gate: the LLM must have echoed the four source
fields, which we cross-check here in code (never trusting the prompt alone). On pass,
the draft is persisted PENDING_REVIEW or APPROVED; on fail it is kept SUPERSEDED for
audit and the workflow routes to a deterministic fallback.
"""

from datetime import date
from typing import Optional

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod

PLACEHOLDERS = ["[INSERT", "[YOUR", "{{", "}}", "PLACEHOLDER", "TBD", "XXX"]
INJECTION = ["ignore previous", "forget instructions", "system:", "assistant:", "<|", "|>"]
VALID_TONES = {"WARM_FRIENDLY", "POLITE_FIRM", "FORMAL_SERIOUS", "STERN_URGENT"}


class ValidateInput(BaseModel):
    invoice_id: str
    client_id: Optional[str] = None
    stage: str
    risk_level: Optional[str] = None
    review_reason: str = "low_risk_auto"
    needs_review: bool = False
    channel: str = "EMAIL"
    subject: str
    body: str
    tone: str
    confidence_score: Optional[float] = None
    invoice_id_used: str
    client_name_used: str
    amount_used: float
    days_overdue_used: int
    model_used: Optional[str] = None
    tokens_used: Optional[int] = None
    latency_ms: Optional[int] = None


class ValidateResult(BaseModel):
    passed: bool
    draft_id: str
    errors: list[str]
    status: str


async def validate_draft(ctx: FunctionContext, data: ValidateInput) -> ValidateResult:
    pod = Pod.from_env()
    inv = pod.table("invoices").get(data.invoice_id)
    if not inv:
        raise ValueError(f"invoice not found: {data.invoice_id}")
    client = pod.table("clients").get(str(inv["client_id"])) if inv.get("client_id") else {}

    errors: list[str] = []

    # 1. shape checks
    if not (5 <= len(data.subject) <= 300):
        errors.append(f"subject_length:{len(data.subject)}")
    if not (50 <= len(data.body) <= 6000):
        errors.append(f"body_length:{len(data.body)}")
    if data.tone not in VALID_TONES:
        errors.append(f"invalid_tone:{data.tone}")

    # 2. hallucination cross-check against the source invoice
    if str(data.invoice_id_used).strip() != str(inv.get("invoice_no", "")).strip():
        errors.append(f"halluc_invoice_no:got={data.invoice_id_used},exp={inv.get('invoice_no')}")
    src_name = str((client or {}).get("name", "")).strip().lower()
    if data.client_name_used.strip().lower() != src_name:
        errors.append(f"halluc_client_name:got={data.client_name_used},exp={src_name}")
    if abs(float(data.amount_used) - float(inv["amount"])) > 0.01:
        errors.append(f"halluc_amount:got={data.amount_used},exp={inv['amount']}")
    src_days = int(inv.get("days_overdue", 0))
    if abs(int(data.days_overdue_used) - src_days) > 1:
        errors.append(f"halluc_days:got={data.days_overdue_used},exp={src_days}")

    # 3. placeholder + injection scan
    body_up = data.body.upper()
    for p in PLACEHOLDERS:
        if p in body_up:
            errors.append(f"placeholder:{p}")
    body_low = data.body.lower()
    for p in INJECTION:
        if p in body_low:
            errors.append(f"injection:{p}")

    passed = len(errors) == 0

    if passed:
        status = "PENDING_REVIEW" if data.needs_review else "APPROVED"
    else:
        status = "SUPERSEDED"  # kept for audit; workflow will make a fallback draft

    # supersede ANY prior active draft for this invoice (not just pending) so an invoice
    # never accumulates duplicate drafts when the pipeline re-runs.
    prior = pod.records.list("drafts", limit=200, filter=[
        {"field": "invoice_id", "op": "eq", "value": data.invoice_id},
    ]).to_dict()["items"]
    to_sup = [r["id"] for r in prior if r.get("status") in ("PENDING_REVIEW", "APPROVED", "AUTO_SENT", "SENT", "FAILED")]
    if to_sup:
        pod.records.bulk_update("drafts", [{"id": i, "status": "SUPERSEDED"} for i in to_sup])

    draft = pod.table("drafts").create({
        "invoice_id": data.invoice_id,
        "client_id": data.client_id or inv.get("client_id"),
        "stage": data.stage,
        "tone": data.tone if data.tone in VALID_TONES else "POLITE_FIRM",
        "channel": data.channel,
        "subject": data.subject[:300],
        "body": data.body[:6000],
        "confidence": data.confidence_score,
        "risk_level": data.risk_level,
        "validation_passed": passed,
        "validation_errors": errors,
        "fallback_used": False,
        "status": status,
        "review_reason": data.review_reason if data.needs_review else ("validation_failed" if not passed else "low_risk_auto"),
        "model_used": data.model_used,
        "tokens_used": data.tokens_used,
        "latency_ms": data.latency_ms,
    })
    draft_id = str(draft["id"])

    pod.table("interactions").create({
        "invoice_id": data.invoice_id,
        "client_id": data.client_id or inv.get("client_id"),
        "draft_id": draft_id,
        "kind": "DRAFT_GENERATED",
        "channel": "SYSTEM",
        "direction": "INTERNAL",
        "summary": f"Draft generated for {inv.get('invoice_no')} ({data.stage}); validation {'passed' if passed else 'FAILED'}",
        "detail": {"errors": errors, "risk_level": data.risk_level},
        "actor_label": "validate_draft",
        "level": "INFO" if passed else "WARN",
    })

    return ValidateResult(passed=passed, draft_id=draft_id, errors=errors, status=status)
