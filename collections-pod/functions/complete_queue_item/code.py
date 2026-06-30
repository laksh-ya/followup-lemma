#input_type_name: CompleteQueueInput
#output_type_name: CompleteQueueResult
#function_name: complete_queue_item

"""Mark a followup_queue row terminal (DONE/ERROR) at the end of a collections-run so
the scan's de-dup and the app's queue view stay accurate. Updating the row does NOT
re-trigger the INSERT-only datastore schedule.
"""

from typing import Optional

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


class CompleteQueueInput(BaseModel):
    queue_id: str
    status: str = "DONE"
    error: Optional[str] = None


class CompleteQueueResult(BaseModel):
    queue_id: str
    status: str


async def complete_queue_item(ctx: FunctionContext, data: CompleteQueueInput) -> CompleteQueueResult:
    pod = Pod.from_env()
    try:
        pod.table("followup_queue").update(data.queue_id, {
            "status": data.status,
            "error": data.error,
        })
    except Exception:
        # queue row may not exist (manual run path) — non-fatal
        pass
    return CompleteQueueResult(queue_id=str(data.queue_id), status=data.status)
