#input_type_name: FlagLegalInput
#output_type_name: FlagLegalResult
#function_name: flag_legal

"""Flag an invoice for the legal/recovery team: set status LEGAL, log the escalation,
and record a notification to the legal audience (delivered over the configured channel
in Phase 3; logged otherwise). Used for 30+ day escalations and reviewer 'flag legal'.
"""

from typing import Optional

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


class FlagLegalInput(BaseModel):
    invoice_id: str
    reason: str = "escalated_30plus"
    note: Optional[str] = None


class FlagLegalResult(BaseModel):
    invoice_id: str
    status: str


async def flag_legal(ctx: FunctionContext, data: FlagLegalInput) -> FlagLegalResult:
    pod = Pod.from_env()
    inv = pod.table("invoices").get(data.invoice_id)
    if not inv:
        raise ValueError(f"invoice not found: {data.invoice_id}")

    pod.table("invoices").update(data.invoice_id, {"status": "LEGAL"})

    no = inv.get("invoice_no", "")
    amt = inv.get("amount")
    cur = inv.get("currency", "INR")
    days = inv.get("days_overdue", 0)

    pod.table("interactions").create({
        "invoice_id": data.invoice_id,
        "client_id": inv.get("client_id"),
        "kind": "LEGAL_FLAGGED",
        "channel": "SYSTEM",
        "direction": "INTERNAL",
        "summary": f"{no} flagged LEGAL ({data.reason}); {cur} {amt} {days} days overdue" + (f" — {data.note}" if data.note else ""),
        "detail": {"reason": data.reason, "note": data.note},
        "actor_label": "flag_legal",
        "level": "ERROR",
    })

    # legal notification (real channel delivery wired in Phase 3; logged either way)
    cfg_items = pod.records.list("pod_config", limit=1).to_dict()["items"]
    channel = (cfg_items[0].get("notify_channel") if cfg_items else None) or "NONE"
    pod.table("interactions").create({
        "invoice_id": data.invoice_id,
        "client_id": inv.get("client_id"),
        "kind": "NOTIFICATION_SENT",
        "channel": channel if channel in ("SLACK", "WHATSAPP") else "SYSTEM",
        "direction": "OUTBOUND",
        "summary": f"[legal] Escalation: {no} is {days} days overdue ({cur} {amt}) — needs manual recovery action",
        "detail": {"audience": "legal", "channel": channel},
        "actor_label": "flag_legal",
        "level": "WARN" if channel == "NONE" else "INFO",
    })

    return FlagLegalResult(invoice_id=str(data.invoice_id), status="LEGAL")
