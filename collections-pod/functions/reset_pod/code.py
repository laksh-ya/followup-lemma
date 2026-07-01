#input_type_name: ResetInput
#output_type_name: ResetResult
#function_name: reset_pod

"""Clear all operational data (customers, invoices, drafts, replies, queue, history) for
a clean slate. Settings (pod_config) are preserved. Used by the app's "Reset data".
"""

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod

TABLES = ["interactions", "drafts", "followup_queue", "promises", "invoices", "clients"]


class ResetInput(BaseModel):
    confirm: bool = False


class ResetResult(BaseModel):
    deleted: dict


async def reset_pod(ctx: FunctionContext, data: ResetInput) -> ResetResult:
    pod = Pod.from_env()
    if not data.confirm:
        return ResetResult(deleted={"note": "pass confirm=true"})
    out = {}
    for t in TABLES:
        ids = [r["id"] for r in pod.records.list(t, limit=5000).to_dict()["items"]]
        if ids:
            pod.records.bulk_delete(t, ids)
        out[t] = len(ids)
    return ResetResult(deleted=out)
