#input_type_name: ResetInput
#output_type_name: ResetResult
#function_name: reset_pod

"""Clear operational data for a clean slate. Settings (pod_config, stats_channels) are
preserved. Two scopes:
  - full (default): every customer, invoice, draft, reply, queue row and history entry.
  - demo_only=true: ONLY the sandbox rows (invoices with demo=true and everything hanging
    off them), so the Mock Mailbox can be cleared without touching real data.
Used by the app's "Reset real data" and the Mock Mailbox "Clear mock data".
"""

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod

TABLES = ["interactions", "drafts", "followup_queue", "promises", "invoices", "clients"]


class ResetInput(BaseModel):
    confirm: bool = False
    demo_only: bool = False


class ResetResult(BaseModel):
    deleted: dict


async def reset_pod(ctx: FunctionContext, data: ResetInput) -> ResetResult:
    pod = Pod.from_env()
    if not data.confirm:
        return ResetResult(deleted={"note": "pass confirm=true"})

    if not data.demo_only:
        out = {}
        for t in TABLES:
            ids = [r["id"] for r in pod.records.list(t, limit=5000).to_dict()["items"]]
            if ids:
                pod.records.bulk_delete(t, ids)
            out[t] = len(ids)
        return ResetResult(deleted=out)

    # demo-only: delete sandbox invoices and everything tied to them, plus any client
    # left with no real (non-demo) invoices.
    invoices = pod.records.list("invoices", limit=5000).to_dict()["items"]
    demo_inv_ids = {str(i["id"]) for i in invoices if i.get("demo")}
    real_client_ids = {str(i.get("client_id")) for i in invoices if not i.get("demo") and i.get("client_id")}
    demo_client_ids = {str(i.get("client_id")) for i in invoices if i.get("demo") and i.get("client_id")}
    orphan_clients = [cid for cid in demo_client_ids if cid not in real_client_ids]

    out = {}
    for t in ["interactions", "drafts", "followup_queue", "promises"]:
        rows = pod.records.list(t, limit=5000).to_dict()["items"]
        ids = [r["id"] for r in rows if str(r.get("invoice_id")) in demo_inv_ids]
        if ids:
            pod.records.bulk_delete(t, ids)
        out[t] = len(ids)
    if demo_inv_ids:
        pod.records.bulk_delete("invoices", list(demo_inv_ids))
    out["invoices"] = len(demo_inv_ids)
    if orphan_clients:
        pod.records.bulk_delete("clients", orphan_clients)
    out["clients"] = len(orphan_clients)
    return ResetResult(deleted=out)
