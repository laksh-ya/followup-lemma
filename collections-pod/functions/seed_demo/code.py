#input_type_name: SeedInput
#output_type_name: SeedResult
#function_name: seed_demo

"""One-click SANDBOX data for the app. Creates a realistic book of customers + invoices
across every escalation stage (deduping customers by email), then enqueues the overdue
ones so the pipeline runs and the Mock Mailbox fills with auto-sent follow-ups, pending
approvals, and legal escalations. Every seeded invoice is tagged demo=true so it stays
isolated in the Mock Mailbox and never mixes with real sends. Idempotent by
email/invoice_no. Nothing is seeded automatically — this only runs when invoked.
"""

from datetime import date, timedelta
from typing import Optional

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


class SeedInput(BaseModel):
    enqueue: bool = True
    count: Optional[int] = None


class SeedResult(BaseModel):
    clients: int
    invoices: int
    enqueued: int


CLIENTS = {
    "northwind": ("Northwind Tech", "accounts@northwindtech.com", "Northwind Tech Pvt. Ltd.", True, "AVERAGE", "Enterprise VIP; Net-45 per MSA."),
    "aurora": ("Aurora Soft", "rajesh.kapoor@aurorasoft.in", "Aurora Soft", False, "GOOD", "Reliable payer; Net-30."),
    "blueocean": ("Blue Ocean Labs", "priya.m@blueoceanlabs.com", "Blue Ocean Labs", False, "AVERAGE", "Marketing retainer."),
    "delta": ("Delta Consult", "vikram@deltaconsult.co.in", "Delta Consult", False, "RISKY", "History of late payments."),
    "meridian": ("Meridian Logix", "suresh@meridianlogix.com", "Meridian Logix", False, "AVERAGE", "SLA-bound; chase weekly."),
    "cindera": ("Cindera AI", "megha.j@cinderaai.com", "Cindera AI", False, "RISKY", "New account; large balance."),
    "helios": ("Helios Health", "tara.k@helioshealth.co", "Helios Health", False, "GOOD", "Healthcare; Net-30."),
}
# (invoice_no, client_key, amount, days_overdue, status)
INVOICES = [
    ("INV-2025-001", "aurora", 45000, 4, "ACTIVE"),
    ("INV-2025-002", "blueocean", 128500, 11, "ACTIVE"),
    ("INV-2025-003", "helios", 42500, 6, "ACTIVE"),
    ("INV-2025-004", "meridian", 180000, 17, "ACTIVE"),
    ("INV-2025-005", "delta", 38000, 25, "ACTIVE"),
    ("INV-2025-006", "northwind", 92000, 18, "ACTIVE"),
    ("INV-2025-007", "cindera", 410000, 27, "ACTIVE"),
    ("INV-2025-008", "blueocean", 64000, 34, "ACTIVE"),
    ("INV-2025-009", "delta", 524000, 42, "ACTIVE"),
    ("INV-2025-010", "aurora", 88000, 3, "PAID"),
    ("INV-2025-011", "meridian", 142000, 9, "DISPUTED"),
    ("INV-2025-012", "helios", 17500, 2, "ACTIVE"),
    ("INV-2025-013", "northwind", 56000, 13, "ACTIVE"),
]


async def seed_demo(ctx: FunctionContext, data: SeedInput) -> SeedResult:
    pod = Pod.from_env()
    count = data.count

    clients_data = {}
    invoices_data = []

    if count is not None and count > 0:
        FIRST_NAMES = ["Rajesh", "Priya", "Vikram", "Suresh", "Megha", "Tara", "Amit", "Neha", "Rohan", "Anjali", "Sanjay", "Kiran", "Aditya", "Pooja", "Arjun", "Deepa"]
        LAST_NAMES = ["Kapoor", "Sharma", "Verma", "Patel", "Reddy", "Nair", "Mehta", "Joshi", "Das", "Rao", "Gupta", "Sen", "Choudhury", "Bose", "Pillai", "Trivedi"]
        COMPANIES = ["Acme", "Apex", "Nova", "Zenith", "Quantum", "Vortex", "Stellar", "Core", "Nexus", "Summit", "Prime", "Matrix", "Krypton", "Aether", "Horizon"]
        INDUSTRIES = ["Tech", "Soft", "Labs", "Consult", "Logix", "AI", "Health", "Solutions", "Ventures", "Systems", "Infotech", "Digital"]
        
        num_clients = max(3, count // 2)
        import random
        for i in range(num_clients):
            rng = random.Random(1000 + i)
            fn = rng.choice(FIRST_NAMES)
            ln = rng.choice(LAST_NAMES)
            name = f"{fn} {ln}"
            comp = rng.choice(COMPANIES)
            ind = rng.choice(INDUSTRIES)
            company = f"{comp} {ind}"
            email = f"{fn.lower()}.{ln.lower()}_{i}@example.com"
            vip = rng.random() < 0.15
            beh = rng.choice(["GOOD", "AVERAGE", "RISKY"])
            notes = f"Generated demo client. Behavior: {beh}."
            if vip:
                notes += " VIP Client."
            clients_data[f"gen_{i}"] = (name, email, company, vip, beh, notes)
            
        for idx in range(count):
            rng = random.Random(2000 + idx)
            client_idx = rng.randint(0, num_clients - 1)
            ck = f"gen_{client_idx}"
            no = f"INV-GEN-{idx+1:03d}"
            amount = rng.randint(5, 50) * 10000
            days = rng.randint(1, 45)
            client_beh = clients_data[ck][4]
            if client_beh == "GOOD":
                status = rng.choice(["ACTIVE", "ACTIVE", "PAID"])
            elif client_beh == "AVERAGE":
                status = rng.choice(["ACTIVE", "ACTIVE", "DISPUTED", "PAID"])
            else:
                status = rng.choice(["ACTIVE", "ACTIVE", "ACTIVE", "DISPUTED", "LEGAL"])
            invoices_data.append((no, ck, amount, days, status))
    else:
        clients_data = CLIENTS
        invoices_data = INVOICES

    existing = pod.records.list("clients", limit=2000).to_dict()["items"]
    by_email = {(c.get("email_norm") or c.get("email", "").lower()): c["id"] for c in existing}
    cid = {}
    for key, (name, email, company, vip, beh, notes) in clients_data.items():
        en = email.lower()
        if en in by_email:
            cid[key] = by_email[en]
        else:
            row = pod.table("clients").create({
                "name": name, "email": email, "email_norm": en, "company": company,
                "vip": vip, "payment_behavior": beh, "notes": notes,
            })
            cid[key] = row["id"]
            by_email[en] = row["id"]

    inv_existing = pod.records.list("invoices", limit=2000).to_dict()["items"]
    by_no = {str(i.get("invoice_no")): i["id"] for i in inv_existing}
    today = date.today()
    n_inv = 0
    inv_ids = {}
    for no, ck, amount, days, status in invoices_data:
        fields = {
            "invoice_no": no, "client_id": cid[ck], "amount": amount, "currency": "INR",
            "due_date": (today - timedelta(days=days)).isoformat(), "status": status,
            "payment_link": f"https://pay.acme.example/{no}", "demo": True,
        }
        if no in by_no:
            pod.table("invoices").update(by_no[no], fields)
            inv_ids[no] = by_no[no]
        else:
            r = pod.table("invoices").create(fields)
            inv_ids[no] = r["id"]
        n_inv += 1

    enq = 0
    if data.enqueue:
        q = pod.records.list("followup_queue", limit=1000).to_dict()["items"]
        queued = {str(x["invoice_id"]) for x in q if x.get("status") in ("QUEUED", "PROCESSING")}
        dr = pod.records.list("drafts", limit=2000).to_dict()["items"]
        drafted = {str(d["invoice_id"]) for d in dr if d.get("status") in ("PENDING_REVIEW", "APPROVED", "AUTO_SENT", "SENT")}
        for no, ck, amount, days, status in invoices_data:
            iid = inv_ids[no]
            if status == "ACTIVE" and days > 0 and iid not in queued and iid not in drafted:
                pod.table("followup_queue").create({"invoice_id": iid, "reason": "manual", "status": "QUEUED"})
                enq += 1

    pod.table("interactions").create({
        "kind": "NOTE", "channel": "SYSTEM", "direction": "INTERNAL",
        "summary": f"Sandbox data seeded (Mock Mailbox) — {len(cid)} customers, {n_inv} invoices, {enq} enqueued",
        "actor_user": ctx.user_id, "actor_label": "seed_demo", "level": "INFO",
    })
    return SeedResult(clients=len(cid), invoices=n_inv, enqueued=enq)
