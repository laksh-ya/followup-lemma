#input_type_name: SeedInput
#output_type_name: SeedResult
#function_name: seed_demo

"""One-click sample data for the app. Creates a realistic book of customers + invoices
across every escalation stage (deduping customers by email), then enqueues the overdue
ones so the pipeline runs and the workspace fills with auto-sent follow-ups, pending
approvals, and legal escalations. Idempotent by email/invoice_no. Nothing is seeded
automatically — this only runs when the operator clicks "Seed random mails".
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
# (invoice_no, client_key, amount, days_overdue, status) — mostly actionable ACTIVE,
# a spread of overdue ages (incl. not-yet-due negatives), a couple resolved/edge cases.
INVOICES = [
    ("INV-2025-001", "aurora", 45000, -5, "ACTIVE"),
    ("INV-2025-002", "blueocean", 128500, 2, "ACTIVE"),
    ("INV-2025-003", "helios", 42500, 6, "ACTIVE"),
    ("INV-2025-004", "meridian", 180000, 12, "ACTIVE"),
    ("INV-2025-005", "delta", 38000, 21, "ACTIVE"),
    ("INV-2025-006", "northwind", 92000, 8, "ACTIVE"),
    ("INV-2025-007", "cindera", 410000, 31, "ACTIVE"),
    ("INV-2025-008", "blueocean", 64000, -2, "ACTIVE"),
    ("INV-2025-009", "aurora", 88000, 4, "PAID"),
    ("INV-2025-010", "meridian", 142000, 9, "ACTIVE"),
    ("INV-2025-011", "helios", 17500, 15, "ACTIVE"),
    ("INV-2025-012", "northwind", 56000, 3, "ACTIVE"),
    ("INV-2025-013", "delta", 524000, 47, "LEGAL"),
]


async def seed_demo(ctx: FunctionContext, data: SeedInput) -> SeedResult:
    pod = Pod.from_env()
    count = data.count

    clients_data = {}
    invoices_data = []

    if count is not None and count > 0:
        import random
        FIRST_NAMES = ["Rajesh", "Priya", "Vikram", "Suresh", "Megha", "Tara", "Amit", "Neha", "Rohan", "Anjali",
                       "Sanjay", "Kiran", "Aditya", "Pooja", "Arjun", "Deepa", "Kavya", "Nikhil", "Ishaan", "Riya",
                       "Manish", "Sneha", "Karthik", "Divya", "Farhan", "Aisha", "Gaurav", "Meera", "Varun", "Ananya"]
        LAST_NAMES = ["Kapoor", "Sharma", "Verma", "Patel", "Reddy", "Nair", "Mehta", "Joshi", "Das", "Rao",
                      "Gupta", "Sen", "Choudhury", "Bose", "Pillai", "Trivedi", "Iyer", "Malhotra", "Sinha", "Kulkarni"]
        COMPANIES = ["Acme", "Apex", "Nova", "Zenith", "Quantum", "Vortex", "Stellar", "Core", "Nexus", "Summit",
                     "Prime", "Matrix", "Krypton", "Aether", "Horizon", "Cobalt", "Onyx", "Vertex", "Lumen", "Ridge"]
        INDUSTRIES = ["Tech", "Soft", "Labs", "Consult", "Logix", "AI", "Health", "Solutions", "Ventures",
                      "Systems", "Infotech", "Digital", "Retail", "Media", "Foods", "Motors", "Pharma"]
        BEHAVIORS = ["GOOD", "GOOD", "AVERAGE", "AVERAGE", "AVERAGE", "RISKY"]  # weighted toward decent payers

        rng = random.Random()  # fresh randomness every click — new customers & invoices each run
        batch = str(rng.randint(1000, 9999))  # unique per run so re-seeding never re-creates the same rows/customers
        num_clients = max(6, int(count * 0.7))  # lots of distinct customers, few repeat invoices
        used_emails = set()
        for i in range(num_clients):
            fn, ln = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
            comp, ind = rng.choice(COMPANIES), rng.choice(INDUSTRIES)
            company = f"{comp} {ind}"
            slug = (comp + ind).lower()
            email = f"{fn.lower()}.{ln.lower()}.{batch}{i}@{slug}.com"
            used_emails.add(email)
            beh = rng.choice(BEHAVIORS)
            vip = rng.random() < 0.12
            notes = ("VIP account. " if vip else "") + {"GOOD": "Reliable payer, Net-30.", "AVERAGE": "Occasional delays.", "RISKY": "History of late payment; watch closely."}[beh]
            clients_data[f"gen_{i}"] = (fn + " " + ln, email, company, vip, beh, notes)

        for idx in range(count):
            ck = f"gen_{rng.randint(0, num_clients - 1)}"
            no = f"INV-{batch}-{idx + 1:03d}"
            # amount: mostly small/mid, occasional large
            amount = rng.choice([rng.randint(1, 9) * 5000, rng.randint(2, 12) * 10000,
                                 rng.randint(3, 9) * 25000, rng.randint(2, 6) * 100000])
            # overdue age: not-yet-due, early, mid, stage-4, and a 30+ escalated slice
            days = rng.choice([rng.randint(-10, 0), rng.randint(1, 7), rng.randint(8, 14),
                               rng.randint(15, 21), rng.randint(22, 30), rng.randint(31, 55)])
            beh = clients_data[ck][4]
            roll = rng.random()
            # mostly ACTIVE/actionable; a small resolved tail; LEGAL rare. DISPUTED is NOT seeded —
            # it only arises when a customer actually disputes (handle_reply), so it never looks fake.
            if roll < (0.06 if beh == "RISKY" else 0.02) and days > 30:
                status = "LEGAL"          # a couple already escalated → Legal tab
            elif roll < 0.16:
                status = "PAID"
            else:
                status = "ACTIVE"         # 30+ ACTIVE = ESCALATED stage → Approvals ▸ Escalation
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
        od = max(0, days)
        # stage thresholds MUST match classify_and_score: 1-7 S1, 8-14 S2, 15-21 S3, 22-30 S4, 30+ ESCALATED
        stage = ("PENDING" if od == 0 else "STAGE_1" if od <= 7 else "STAGE_2" if od <= 14
                 else "STAGE_3" if od <= 21 else "STAGE_4" if od <= 30 else "ESCALATED")
        risk = "HIGH" if amount >= 200000 else ("MEDIUM" if amount >= 75000 else "LOW")
        fields = {
            "invoice_no": no, "client_id": cid[ck], "amount": amount, "currency": "INR",
            "due_date": (today - timedelta(days=days)).isoformat(), "status": status,
            "days_overdue": od, "stage": stage, "risk_level": risk,
            "payment_link": f"https://pay.acme.example/{no}",
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
        import random
        eq_rng = random.Random(11)
        q = pod.records.list("followup_queue", limit=1000).to_dict()["items"]
        queued = {str(x["invoice_id"]) for x in q if x.get("status") in ("QUEUED", "PROCESSING")}
        dr = pod.records.list("drafts", limit=2000).to_dict()["items"]
        drafted = {str(d["invoice_id"]) for d in dr if d.get("status") in ("PENDING_REVIEW", "APPROVED", "AUTO_SENT", "SENT")}
        for no, ck, amount, days, status in invoices_data:
            iid = inv_ids[no]
            # only overdue ACTIVE invoices are eligible; process ~65% so some stay fresh/untouched
            if status == "ACTIVE" and days > 0 and iid not in queued and iid not in drafted:
                if eq_rng.random() < 0.65:
                    pod.table("followup_queue").create({"invoice_id": iid, "reason": "manual", "status": "QUEUED"})
                    enq += 1

    pod.table("interactions").create({
        "kind": "NOTE", "channel": "SYSTEM", "direction": "INTERNAL",
        "summary": f"Seeded sample data — {len(cid)} customers, {n_inv} invoices ({enq} queued for the agent)",
        "actor_user": ctx.user_id, "actor_label": "seed_demo", "level": "INFO",
    })
    return SeedResult(clients=len(cid), invoices=n_inv, enqueued=enq)
