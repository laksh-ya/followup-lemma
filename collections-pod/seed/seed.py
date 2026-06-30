#!/usr/bin/env python3
"""Seed the collections-agent pod with a realistic demo dataset.

Creates clients + invoices spanning every escalation stage so that, after a scan,
the pod shows: several auto-sent follow-ups, a few drafts pending finance-lead
approval, and 30+ day accounts flagged to legal. Idempotent-ish: run against a clean
pod (the README documents the reset step).

Usage: python3 seed.py   (requires LEMMA_ORG_ID + LEMMA_POD_ID env or active context)
"""
import json
import subprocess
from datetime import date, timedelta


def lemma(*args, data=None):
    cmd = ["lemma", "--output", "json", *args]
    if data is not None:
        cmd += ["--data", json.dumps(data)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {out.stderr or out.stdout}")
    return json.loads(out.stdout)


def create(table, row):
    return lemma("records", "create", table, data=row)


def due(days):
    return (date.today() - timedelta(days=days)).isoformat()


# current user → finance lead + legal (single-member demo)
me = lemma("auth", "status")
user_id = me.get("id") or me.get("user_id")

# pod_config: ensure one row with safe demo defaults (in-app, no external channel)
cfgs = lemma("query", "run", "select id from pod_config").get("items", [])
cfg_fields = {
    "auto_dispatch": True, "human_in_loop": True, "review_stage4": True,
    "review_high_risk": True, "email_enabled": False, "notify_channel": "NONE",
    "company_name": "Acme Financial Services", "sender_identity": "Acme Finance team",
    "finance_lead_member_id": user_id, "legal_member_id": user_id,
}
if cfgs:
    lemma("records", "update", "pod_config", cfgs[0]["id"], data=cfg_fields)
else:
    create("pod_config", {"singleton": "main", **cfg_fields})

CLIENTS = {
    "northwind": {"name": "Northwind Tech", "email": "accounts@northwindtech.com",
                  "company": "Northwind Tech Pvt. Ltd.", "vip": True,
                  "payment_behavior": "AVERAGE", "notes": "Enterprise VIP; Net-45 per MSA; named finance contact."},
    "aurora": {"name": "Aurora Soft", "email": "rajesh.kapoor@aurorasoft.in",
               "company": "Aurora Soft", "vip": False, "payment_behavior": "GOOD",
               "notes": "Reliable payer; Net-30."},
    "blueocean": {"name": "Blue Ocean Labs", "email": "priya.m@blueoceanlabs.com",
                  "company": "Blue Ocean Labs", "vip": False, "payment_behavior": "AVERAGE",
                  "notes": "Marketing retainer."},
    "delta": {"name": "Delta Consult", "email": "vikram@deltaconsult.co.in",
              "company": "Delta Consult", "vip": False, "payment_behavior": "RISKY",
              "notes": "History of late payments; watch closely."},
    "meridian": {"name": "Meridian Logix", "email": "suresh@meridianlogix.com",
                 "company": "Meridian Logix", "vip": False, "payment_behavior": "AVERAGE",
                 "notes": "SLA-bound; chase weekly."},
    "cindera": {"name": "Cindera AI", "email": "megha.j@cinderaai.com",
                "company": "Cindera AI", "vip": False, "payment_behavior": "RISKY",
                "notes": "New account; large balance."},
    "helios": {"name": "Helios Health", "email": "tara.k@helioshealth.co",
               "company": "Helios Health", "vip": False, "payment_behavior": "GOOD",
               "notes": "Healthcare; Net-30."},
}

cid = {}
for key, c in CLIENTS.items():
    cid[key] = create("clients", c)["id"]
print(f"clients: {len(cid)}")

# (invoice_no, client_key, amount, days_overdue, status)
INVOICES = [
    ("INV-2025-001", "aurora", 45000, 4, "ACTIVE"),      # STAGE_1 LOW  -> auto
    ("INV-2025-002", "blueocean", 128500, 11, "ACTIVE"), # STAGE_2 MED  -> auto
    ("INV-2025-003", "helios", 42500, 6, "ACTIVE"),      # STAGE_1 LOW  -> auto
    ("INV-2025-004", "meridian", 180000, 17, "ACTIVE"),  # STAGE_3 HIGH -> review
    ("INV-2025-005", "delta", 38000, 25, "ACTIVE"),      # STAGE_4      -> review
    ("INV-2025-006", "northwind", 92000, 18, "ACTIVE"),  # STAGE_3 VIP  -> auto
    ("INV-2025-007", "cindera", 410000, 27, "ACTIVE"),   # STAGE_4 HIGH -> review
    ("INV-2025-008", "blueocean", 64000, 34, "ACTIVE"),  # ESCALATED    -> legal
    ("INV-2025-009", "delta", 524000, 42, "ACTIVE"),     # ESCALATED    -> legal
    ("INV-2025-010", "aurora", 88000, 3, "PAID"),        # paid (dashboard)
    ("INV-2025-011", "meridian", 142000, 9, "DISPUTED"), # disputed (dashboard)
    ("INV-2025-012", "helios", 17500, 2, "ACTIVE"),      # STAGE_1 LOW  -> auto
]

n = 0
for no, ck, amount, days, status in INVOICES:
    create("invoices", {
        "invoice_no": no, "client_id": cid[ck], "amount": amount,
        "currency": "INR", "due_date": due(days), "status": status,
        "payment_link": f"https://pay.acme.example/{no}",
    })
    n += 1
print(f"invoices: {n}")
print("done. run scan_overdue (force) to process them.")
