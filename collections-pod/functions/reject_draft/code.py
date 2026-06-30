#input_type_name: RejectInput
#output_type_name: RejectResult
#function_name: reject_draft

"""Mark a draft REJECTED by a reviewer and log it. No message is sent."""

from typing import Optional

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


class RejectInput(BaseModel):
    draft_id: str
    note: Optional[str] = None


class RejectResult(BaseModel):
    draft_id: str
    status: str


async def reject_draft(ctx: FunctionContext, data: RejectInput) -> RejectResult:
    pod = Pod.from_env()
    draft = pod.table("drafts").get(data.draft_id)
    pod.table("drafts").update(data.draft_id, {
        "status": "REJECTED",
        "reviewer_note": data.note,
    })
    if draft:
        pod.table("interactions").create({
            "invoice_id": draft.get("invoice_id"),
            "client_id": draft.get("client_id"),
            "draft_id": data.draft_id,
            "kind": "HUMAN_REJECTED",
            "channel": "SYSTEM",
            "direction": "INTERNAL",
            "summary": "Reviewer rejected the draft; no message sent" + (f" — {data.note}" if data.note else ""),
            "detail": {"note": data.note},
            "actor_label": "reviewer",
            "level": "WARN",
        })
    return RejectResult(draft_id=str(data.draft_id), status="REJECTED")
