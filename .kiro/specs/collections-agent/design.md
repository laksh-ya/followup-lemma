# Design — Collections Agent (Lemma pod)

> AI accounts-receivable follow-up agent, rebuilt on Lemma. Escalation-aware
> follow-up drafting with anti-hallucination validation, human approval, a real
> client/history data model, multi-role team visibility, and multi-channel
> notifications — collapsing the old FastAPI + LangGraph + Celery + Redis +
> SQLite + Sheets + Next.js stack into one pod.

Pod name: `collections-agent`

---

## 1. Problem & framing

Finance teams (and founders/freelancers who chase their own invoices) pursue
overdue payments by hand: inconsistent tone, no audit trail, delayed escalations,
hours of copy-paste. The hard part isn't writing one email — it's writing the
*right* email, for the *right* client, at the *right* escalation level, every day,
at scale, **without hallucinating invoice data** and without firing a legal threat
at someone three days late.

The old app proved the core loop. This rebuild fixes its real weaknesses (no client
entity, single-user, fragile Sheets-as-DB, fake confidence, outbound-only) and adds
what Lemma makes cheap: a real relational model with history, team roles, a native
human-approval step, built-in RAG grounding, inbound replies, and WhatsApp/Slack
notifications.

---

## 2. Goals / non-goals

**Goals**
- Reproduce every meaningful capability of the old app (see §13 parity matrix) on Lemma.
- A real data model: `clients` ↔ `invoices` with per-client history.
- Team visibility: shared tables + roles (collector / finance-lead / legal / admin), so the demo shows multiple POVs working the same state — the Google-Sheet replacement.
- Native human-in-the-loop approval (workflow FORM), not a hand-rolled queue.
- Deterministic risk scoring (replaces the LLM's fake self-reported confidence).
- Anti-hallucination validation kept verbatim in spirit (the old app's crown jewel).
- Multi-channel notifications: daily digest, legal-escalation ping, strict customer notices (WhatsApp/Slack).
- Configurable knobs throughout (Settings parity): auto-dispatch, human-in-loop, channel, thresholds.
- Everything degrades gracefully with **zero connectors** (in-app-only mode) so the core demo can't break.

**Non-goals (for the hackathon build)**
- Real production email deliverability (SPF/DKIM/DMARC) — use Gmail connector or in-app.
- Payment gateway integration — `payment_link` stays a stored URL.
- Multi-currency math — store currency string, no FX.
- Replacing Lemma's run history with a custom observability stack (we surface it instead).

---

## 3. Build phases (we ship the WHOLE product — phases are just order)

This is **not** deadline-cut scope. We build the complete product, including all
notifications and inbound replies. Phases are dependency order so each layer is
verified before the next is wired on (per pod-design's bottom-up testing).

**Phase 1 — core data + reasoning:** tables + seed → `classify_and_score` →
`collections-drafter` (RAG-grounded) → `validate_draft` → `collections-run` workflow
with the finance-lead approval FORM → in-app dispatch.

**Phase 2 — the operator product:** the full `collections-app` — dashboard, invoice
queue, client timeline, approvals inbox, legal queue, sent log, audit, settings; live
WebSocket updates; role-aware views.

**Phase 3 — automation & reach:** `scan_overdue` + schedules (daily scan + reactive
fan-out), Gmail send + inbound reply triage (closes the loop), WhatsApp/Slack surface
for daily digest + legal-escalation ping + strict customer notices, promise-to-pay
tracking + re-check.

Every phase leaves a working, demoable system; we don't stop until all three are done
and verified end-to-end.

---

## 4. Roles & permissions

Pod members hold Lemma roles; the app shows role-scoped views over **shared** data.

| Conceptual role | Lemma member role | Sees / does |
|---|---|---|
| collector | `POD_USER` | Owns accounts, reviews/edits drafts, triggers follow-ups |
| finance_lead | `POD_EDITOR` | Approves stage-4 / high-risk notices (approval inbox) |
| legal | `POD_USER` | Works the Legal queue, receives escalation pings |
| admin | `POD_ADMIN` | Everything; manages config & members |

**RLS decision: all domain tables are SHARED (`enable_rls: false`).** This is the
correct Lemma pattern here — it's one team operating loop, and the workflow must
read/write across all rows regardless of who started the run (RLS would scope a run
to its owner). "My accounts" is expressed via an `owner` USER column + an app
filter, **not** RLS.

> Demo honesty: with a single pod member, all four roles resolve to that one user;
> the app simulates role views via filters, and the approval FORM is assigned to the
> pod owner. Inviting real members (a 2-minute action) upgrades this to genuine
> multi-user with no code change. Approver/legal member ids are stored in
> `pod_config` (§5.5) so assignment is data-driven and portable.

---

## 5. Data model (tables)

Conventions: `id`, `created_at`, `updated_at` are auto-managed system columns (and
`user_id` only on RLS tables — we have none). All tables below are **shared**.
FKs are `UUID` columns referencing another table's `id`.

### 5.1 `clients`
The relationship entity the old app never had.

| Column | Type | Notes |
|---|---|---|
| name | TEXT | required |
| email | TEXT | primary contact email |
| phone | TEXT | nullable; for WhatsApp notices |
| company | TEXT | nullable |
| vip | BOOLEAN | default `false`; softens tone, raises review bar |
| payment_behavior | ENUM(`GOOD`,`AVERAGE`,`RISKY`) | default `AVERAGE`; feeds risk score |
| owner | USER | assigned collector; nullable |
| notes | TEXT | freeform; agent reads as context |

Edge cases: an invoice referencing a missing client is auto-linked/created on
ingest by `(name,email)`; duplicate clients de-duped by email on upsert.

### 5.2 `invoices`
The unit of work. One client has many invoices.

| Column | Type | Notes |
|---|---|---|
| invoice_no | TEXT | human id e.g. `INV-2025-001` (display; `id` is the real PK) |
| client_id | UUID → clients | required |
| amount | FLOAT | > 0, sanity-capped at 10,000,000 |
| currency | TEXT | default `INR` |
| due_date | DATE | source of truth for overdue math |
| days_overdue | INTEGER | **derived**, recomputed each scan from `due_date` |
| stage | ENUM(`PENDING`,`STAGE_1`,`STAGE_2`,`STAGE_3`,`STAGE_4`,`ESCALATED`) | from days_overdue |
| status | ENUM(`ACTIVE`,`PAID`,`DISPUTED`,`LEGAL`,`PAUSED`) | lifecycle |
| risk_level | ENUM(`LOW`,`MEDIUM`,`HIGH`) | nullable; from `classify_and_score` |
| payment_link | TEXT | nullable |
| followup_count | INTEGER | default 0; bumped only on real delivery |
| last_followup_at | DATETIME | nullable |
| last_processed_at | DATETIME | nullable; de-dup guard |
| notes | TEXT | nullable |

Stage map (unchanged from old app): 1–7 `STAGE_1`, 8–14 `STAGE_2`, 15–21 `STAGE_3`,
22–30 `STAGE_4`, 30+ `ESCALATED`, ≤0 `PENDING`.

Edge cases: `due_date` in the future → `PENDING`, skipped by scan. `PAID/PAUSED/LEGAL`
skipped by scan. `days_overdue` never trusted from import — always recomputed.

### 5.3 `drafts`
The generated message + its full lifecycle (this single table replaces the old
`human_queue` **and** `sent_emails` — see §13 for the alternative split option).

| Column | Type | Notes |
|---|---|---|
| invoice_id | UUID → invoices | required |
| client_id | UUID → clients | denormalized for app convenience |
| stage | ENUM (same as invoice) | snapshot at generation |
| tone | ENUM(`WARM_FRIENDLY`,`POLITE_FIRM`,`FORMAL_SERIOUS`,`STERN_URGENT`) | |
| channel | ENUM(`EMAIL`,`WHATSAPP`) | default `EMAIL` |
| subject | TEXT | |
| body | TEXT | |
| confidence | FLOAT | agent self-report (secondary signal) |
| risk_level | ENUM(`LOW`,`MEDIUM`,`HIGH`) | from function (primary gate) |
| validation_passed | BOOLEAN | |
| validation_errors | JSON | nullable list |
| fallback_used | BOOLEAN | default `false` |
| status | ENUM(`PENDING_REVIEW`,`APPROVED`,`AUTO_SENT`,`SENT`,`REJECTED`,`FAILED`,`SUPERSEDED`) | |
| review_reason | TEXT | `low_risk_auto`/`high_risk`/`stage_4`/`escalated`/`validation_failed`/`auto_dispatch_off` |
| reviewer | USER | nullable |
| reviewer_note | TEXT | nullable |
| edited_by_human | BOOLEAN | default `false` |
| model_used | TEXT | nullable (observability) |
| tokens_used | INTEGER | nullable |
| latency_ms | INTEGER | nullable |
| sent_at | DATETIME | nullable |
| provider | TEXT | `gmail`/`in_app`/`whatsapp` |
| provider_message_id | TEXT | nullable |
| error_message | TEXT | nullable |

App views derive from `status`: Approvals inbox = `PENDING_REVIEW`; Sent log =
`SENT`/`AUTO_SENT`. Re-scan supersedes a prior `PENDING_REVIEW` draft for the same
invoice (status → `SUPERSEDED`) so the queue never accumulates duplicates.

### 5.4 `interactions`
The unified, append-only timeline — replaces the old `audit_entries` +
`activity_events` + parts of `sent_emails`. Powers the per-client history and the
live feed.

| Column | Type | Notes |
|---|---|---|
| invoice_id | UUID → invoices | nullable |
| client_id | UUID → clients | nullable |
| draft_id | UUID → drafts | nullable |
| kind | ENUM (below) | required |
| channel | ENUM(`EMAIL`,`WHATSAPP`,`SLACK`,`SYSTEM`) | nullable |
| direction | ENUM(`OUTBOUND`,`INBOUND`,`INTERNAL`) | nullable |
| summary | TEXT | human-readable line |
| detail | JSON | nullable (raw payload, validation errors, etc.) |
| actor_user | USER | nullable (null = system/agent) |
| actor_label | TEXT | e.g. `collections-drafter` |
| level | ENUM(`INFO`,`SUCCESS`,`WARN`,`ERROR`) | default `INFO` |

`kind` ∈ `INVOICE_INGESTED, DRAFT_GENERATED, EMAIL_SENT, EMAIL_FAILED,
REPLY_RECEIVED, STATUS_CHANGE, STAGE_ESCALATED, HUMAN_APPROVED, HUMAN_REJECTED,
FALLBACK_USED, LEGAL_FLAGGED, NOTIFICATION_SENT, PROMISE_MADE, PROMISE_BROKEN, NOTE`.

### 5.5 `pod_config` (single logical row) — Settings parity
Runtime knobs, editable from the app's Settings page (replaces the old
`config_router` + env mutation).

| Column | Type | Default | Controls |
|---|---|---|---|
| auto_dispatch | BOOLEAN | `true` | off → every draft routes to review |
| human_in_loop | BOOLEAN | `true` | off → only escalated/validation-fail go to human |
| review_stage4 | BOOLEAN | `true` | stage-4 always needs approval |
| review_high_risk | BOOLEAN | `true` | HIGH risk always needs approval |
| email_enabled | BOOLEAN | `false` | false = in-app only ("mock"); true = send via Gmail |
| notify_channel | ENUM(`NONE`,`WHATSAPP`,`SLACK`) | `NONE` | digest + escalation channel |
| company_name | TEXT | "Acme Financial Services" | prompt + signature |
| sender_identity | TEXT | "Finance team" | email signature |
| finance_lead_member_id | TEXT | null | approval FORM assignee |
| legal_member_id | TEXT | null | escalation routing |

### 5.6 `followup_queue` (choreography) — Ring 1
Decouples "scan picked this" from "process this," enabling the reactive fan-out
demo and clean retries.

| Column | Type | Notes |
|---|---|---|
| invoice_id | UUID → invoices | required |
| reason | TEXT | `overdue_scan`/`manual`/`resend_failed`/`reply_followup` |
| status | ENUM(`QUEUED`,`PROCESSING`,`DONE`,`ERROR`) | |
| run_id | TEXT | nullable; the collections-run id |
| error | TEXT | nullable (dead-letter equivalent) |

### 5.7 `promises` (Ring 2) — promise-to-pay tracking
| Column | Type | Notes |
|---|---|---|
| invoice_id | UUID → invoices | required |
| client_id | UUID → clients | |
| promised_date | DATE | |
| promised_amount | FLOAT | nullable |
| status | ENUM(`OPEN`,`KEPT`,`BROKEN`,`CANCELLED`) | |
| source | TEXT | `reply`/`manual` |

---

## 6. Files & RAG (`/knowledge`)
Uploaded documents are auto-indexed (the pod is the RAG system):
- `/knowledge/collection_policy.md` — escalation tone rules, do/don't, legal thresholds.
- `/knowledge/payment_terms.md` — net-30/net-45 terms, late-fee policy.
- `/knowledge/contracts/<client>.pdf` — a couple of sample MSAs so drafts can cite real clauses.

`collections-drafter` grounds emails in these docs ("per the net-30 clause in your
MSA…"). Big upgrade over the old app, which had none.

> **Environment note (grounding strategy):** this local stack cannot run the Kreuzberg
> document-extraction service (its image `ghcr.io/kreuzberg-dev/kreuzberg` is
> registry-gated), so the semantic **search index** is unavailable. We therefore ground
> by **direct file read by path** (`files cat` / `download_markdown` on the `.md`
> docs, which return raw text with no extraction step) rather than semantic search.
> The knowledge set is small and addressed by known paths
> (`/knowledge/collection_policy.md`, `/knowledge/payment_terms.md`,
> `/knowledge/contracts/<client>.md`), so direct read is reliable and arguably more
> deterministic. On a stack with Kreuzberg enabled, the same agent can additionally use
> `search` with zero code change.

---

## 7. Functions (deterministic — kept off the LLM)

Each lists its grants (zero-access-by-default; every table/connector is explicit).

### 7.1 `classify_and_score`
- **In:** `invoice_id`
- **Does:** recompute `days_overdue` from `due_date`; set `stage`; compute
  `risk_level` deterministically (below); write the invoice.
- **Out:** `{ stage, days_overdue, risk_level, status }`
- **Grants:** `invoices` r/w, `clients` r
- **Risk formula (the honest replacement for fake confidence):**
  `score = w_amount·norm(amount) + w_days·norm(days_overdue) + w_behavior·behavior_weight + vip_bump`
  → bucketed `LOW/MEDIUM/HIGH`. `behavior_weight`: GOOD 0, AVERAGE 0.5, RISKY 1.0.
  Transparent, explainable, tunable — not LLM vibes. (Weights live as constants;
  optional: surface them in `pod_config` later.)

### 7.2 `validate_draft` (the crown jewel, ported)
- **In:** the agent's `GeneratedEmail` + `invoice_id`
- **Does:** (1) schema/length/tone-enum check; (2) **4-field hallucination
  cross-check** (`invoice_id`, `client_name` case-insensitive, `amount` ±0.01,
  `days_overdue` ±1) against the source invoice; (3) placeholder scan
  (`[INSERT`, `TBD`, `PLACEHOLDER`, `{{`, `XXX`); (4) prompt-injection scan on any
  echoed client text. Persists the `drafts` row with `validation_passed` + errors.
- **Out:** `{ passed, errors[], draft_id }`
- **Grants:** `invoices` r, `drafts` w

### 7.3 `dispatch_followup`
- **In:** `draft_id`
- **Does:** if `email_enabled` AND Gmail connected → send via Gmail; else mark
  in-app `AUTO_SENT`/`SENT`. Always: write `interactions` (EMAIL_SENT/FAILED), bump
  `followup_count` + `last_followup_at` **only on success**, update draft status.
- **Out:** `{ status, provider, message_id? }`
- **Grants:** `drafts` r/w, `invoices` r/w, `interactions` w, `gmail` use *(optional)*

### 7.4 `make_fallback_draft`
- **In:** `invoice_id`, `stage`, `reason`
- **Does:** deterministic per-stage template (ported from old `mock.py`), persisted
  as a `PENDING_REVIEW` draft with `fallback_used=true` — always human-reviewed.
- **Grants:** `invoices` r, `clients` r, `drafts` w, `interactions` w

### 7.5 `flag_legal`
- **In:** `invoice_id`, `reason`
- **Does:** status → `LEGAL`; `interactions` LEGAL_FLAGGED; notify legal (via `notify`).
- **Grants:** `invoices` r/w, `interactions` w (+ notify's grants)

### 7.6 `notify`
- **In:** `audience` (`legal`/`team`/`client`), `message`, optional `channel`
- **Does:** resolve channel from `pod_config`; send via WhatsApp/Slack surface or
  Gmail; if no channel configured, log `NOTIFICATION_SENT` (level WARN, "channel not
  configured") so the flow never breaks. Always writes an `interactions` row.
- **Grants:** `interactions` w, connector(s) use *(optional)*

### 7.7 `scan_overdue`
- **In:** none (or `{ force, failed_only }`)
- **Does:** recompute all ACTIVE invoices; for each that's overdue, not
  PAID/PAUSED/LEGAL, and not processed today (unless `force`), INSERT a
  `followup_queue` row. `failed_only` re-queues invoices whose last draft `FAILED`.
- **Out:** `{ enqueued, skipped }`
- **Grants:** `invoices` r/w, `followup_queue` w, `interactions` w

### 7.8 `metrics_snapshot` (app helper)
- Aggregates totals (overdue, paid, sent today, pending review, legal, errors),
  stage distribution, risk distribution. Used by the dashboard.
- **Grants:** `invoices` r, `drafts` r, `interactions` r

*(Ring 2)* `handle_reply` (apply triage result: set status / create promise / notify),
`recheck_promises` (mark BROKEN + re-queue).

---

## 8. Agents (judgment)

### 8.1 `collections-drafter` (core)
- **Input mapping:** invoice fields, client fields, recent history summary, `stage`,
  `tone`, optional `tone_override`.
- **Behavior:** searches `/knowledge` (SUBTREE) for relevant policy/contract terms;
  writes a stage-appropriate, client-aware email; must echo the four source fields
  for the hallucination check.
- **`output_schema` (`GeneratedEmail`):**
  `subject` (5–200), `body` (50–3000), `tone` (enum), `escalation_stage` (1–4),
  `confidence_score` (0–1), `invoice_id_used`, `client_name_used`, `amount_used`,
  `days_overdue_used`, `reasoning`, `policy_citations` (string[]).
- **Grants:** `/knowledge` file read, `invoices` r, `clients` r, `interactions` r.
- **Why one agent:** classify-tone + retrieve-policy + draft is one cohesive
  judgment pass (heuristic #1). Risk and validation are deterministic → functions.

### 8.2 `reply-triager` (Ring 2, orthogonal judgment)
- **Input:** raw inbound email text + invoice context.
- **`output_schema`:** `category` (`PROMISE_TO_PAY`/`DISPUTE`/`PAID_CLAIM`/`QUESTION`/`OTHER`),
  `promised_date?`, `promised_amount?`, `sentiment`, `suggested_status`, `summary`.
- **Grants:** `invoices` r, `clients` r.

### 8.3 `digest-writer` (Ring 2)
- **Input:** `metrics_snapshot` output.
- **Output:** a tight WhatsApp/Slack-ready daily summary. (Could be a templated
  function instead — agent gives nicer prose; decide by time.)

---

## 9. Workflow `collections-run` (the heart)

Per-invoice pipeline. Started per `followup_queue` row (DATASTORE event) or manually.
Native human approval replaces the old `human_queue` table.

```
start (invoice_id)
  → classify_and_score (FUNCTION)
  → after_classify (DECISION)
        rule: stage == 'ESCALATED' || status != 'ACTIVE'  → flag_legal → END
        else (default)                                     → drafter
  → collections-drafter (AGENT)            # GeneratedEmail
  → validate_draft (FUNCTION)              # persists draft, returns {passed,errors}
  → after_validate (DECISION)
        rule: validate_draft.passed == `true`              → route_dispatch
        else (default)                                     → make_fallback_draft → END   # pending human
  → route_dispatch (DECISION)
        rule: needs_review == `true`                       → review (FORM)
        else (default = auto)                              → dispatch_followup → END
  → review (FORM, assignee = finance_lead_member_id)       # approve / edit / reject
  → after_review (DECISION)
        rule: review.decision == 'approve'                 → dispatch_followup → END
        rule: review.decision == 'reject'                  → reject_draft → END
        else (default = 'flag_legal')                      → flag_legal → END
```

`needs_review` is computed in `validate_draft`/`classify` and surfaced for the
decision: `risk_level == HIGH` OR `stage == STAGE_4` OR `auto_dispatch == false`.
The FORM `input_schema`: `decision` (enum approve/reject/flag_legal), `edited_subject?`,
`edited_body?`, `note?` — and can pre-fill the editable body from the draft via a
dynamic binding.

**Decision-node hygiene (per workflows.md footgun):** every branch has its own
explicit edge to a distinct node; the default/else edge is listed first and never
shares a target with a rule.

**Bounded retry choice (documented option):** rather than looping the agent N times
(awkward in a DAG), a failed validation goes straight to `make_fallback_draft` +
human review. Simpler, deterministic, and still "never fails silently." If we want a
single retry, add one extra `drafter→validate` pair gated by a `retry==false` rule.
Recommendation: ship the fallback path; add retry only if time permits.

---

## 10. Schedules & choreography

- **`daily-scan`** — `TIME` cron (09:00 IST) → workflow with one `scan_overdue`
  FUNCTION node. Enqueues `followup_queue` rows.
- **`followup-dispatch`** — `DATASTORE_EVENT` on `followup_queue` (INSERT) →
  `collections-run`. Read the invoice id from `start.metadata.record_id`'s row (the
  queue row's `invoice_id` lands in `start.payload`). This is the reactive fan-out —
  great live demo (watch the queue drain as drafts appear).
- *(Ring 2)* **`daily-digest`** — `TIME` → `digest-writer` → `notify(team)`.
- *(Ring 2)* **`promise-recheck`** — `TIME` → `recheck_promises`.
- *(Ring 2)* **`gmail-inbound`** — `WEBHOOK` (Gmail trigger) → reply workflow
  (`reply-triager` → `handle_reply`).

> Guardrail: `scan_overdue` writes `followup_queue` (not `invoices`), and
> `collections-run` writes `invoices`/`drafts` — so the DATASTORE trigger on
> `followup_queue` can't loop back on itself. Pause test schedules after verifying.
>
> **Simpler fallback if time runs short:** drop `followup_queue` + the DATASTORE
> schedule; expose a "Run scan" button + per-invoice "Process" button in the app that
> start `collections-run` directly. Less magical, fully demoable.

---

## 11. Connectors & surfaces (Ring 2)

- **Gmail connector** — `dispatch_followup` sends; `gmail-inbound` WEBHOOK ingests
  replies. Demo safety: send only to seeded test addresses; `email_enabled` gates it.
- **WhatsApp / Slack surface** — `notify` posts: daily digest (team), legal
  escalation (legal), and strict customer notices (client). Telegram/Slack work
  locally without a public webhook (long-poll / socket mode) — easiest to demo.

Connectors need a Composio key + per-account auth and **don't round-trip in the
bundle** — setup is recorded in the README. Everything works without them
(`email_enabled=false`, `notify_channel=NONE`) so Ring 1 is connector-free.

---

## 12. App `collections-app`

Browser UI over the same tables (live via `datastore.watchChanges`, never polling).
Role-aware. Mirrors the old 8-page dashboard.

| View | Content | Role emphasis |
|---|---|---|
| Dashboard | KPIs, stage chart, risk chart, live activity feed | all |
| Invoices | Filterable queue (stage/status/owner/risk), pills for last delivery | collector |
| Client detail | Profile + **full interaction timeline** + open invoices | all |
| Approvals | `drafts` where `PENDING_REVIEW`; approve / edit / reject / flag actions | finance_lead |
| Legal queue | invoices `status=LEGAL` + escalation context | legal |
| Sent log | `drafts` `SENT`/`AUTO_SENT` with full body + provider + confidence | all |
| Audit / activity | `interactions` feed, filterable | all/admin |
| Settings | `pod_config` toggles; per-invoice tone override + regenerate; "Run scan" | admin |

Live demo magic: open Approvals, approve a stern notice, watch the Sent log + client
timeline update in real time without refresh.

---

## 13. Old → New parity (nothing lost)

| Old (FastAPI/LangGraph/Celery/Next) | New (Lemma) |
|---|---|
| CSV/Excel/Sheets ingest | App CSV upload + seed → `clients`/`invoices` (Sheets dropped) |
| LangGraph 10-node pipeline | `collections-run` workflow |
| `classify_stage` | `classify_and_score` function |
| Prompt registry per stage | drafter agent instructions + tone map |
| Anti-hallucination check | `validate_draft` function (ported) |
| LLM self-reported confidence | deterministic `risk_level` (+ confidence as secondary) |
| Retry loop + fallback template | validation-fail → `make_fallback_draft` + human |
| Human review queue (approve/edit/reject/regenerate/flag) | FORM + `drafts` actions + `flag_legal` + re-run |
| Email modes mock/sandbox/live | `pod_config.email_enabled` + Gmail (in-app = "mock") |
| `sent_emails` source of truth | `drafts` (SENT/AUTO_SENT) + `interactions` |
| Dead-letter queue + retry | `followup_queue` ERROR + run history; resend-failed scan |
| Google Sheets writeback (team view) | shared tables + app = the live team view |
| Langfuse traces | Lemma run history + draft model/tokens/latency + activity feed |
| Settings runtime controls | `pod_config` + Settings page |
| Activity feed / audit log | `interactions` |
| KPIs/metrics | `metrics_snapshot` |
| Priority routing (stage 3+) | `followup_queue.reason`/priority (minor; optional) |
| Rate limiting / auth / PII mask | platform (RLS+roles) + input sanitize in validate/ingest |

New beyond the old app: real client entity + history, team roles/multi-POV,
inbound replies (loop closed), WhatsApp/Slack notifications, RAG-grounded emails,
promise-to-pay.

---

## 14. Edge cases & failure handling

- **Bad import rows** (invalid email, negative/zero amount, malformed id) → rejected
  with per-row errors; valid rows still import.
- **Missing client on invoice import** → auto-link/create by `(name,email)`.
- **Future / not-yet-due** → `PENDING`, skipped by scan.
- **Paid/Paused/Legal** → skipped by scan.
- **Duplicate scan / concurrent manual run** → `last_processed_at` + queue `status`
  guard; a fresh draft supersedes a prior `PENDING_REVIEW` one.
- **Agent malformed output** → `output_schema` enforces; backstopped by
  `validate_draft` → fallback path.
- **Hallucinated fields** → cross-check fails → fallback draft + human (never sent).
- **Invoice paid mid-flight** → status check before dispatch aborts the send.
- **Connector down / not configured** → `dispatch_followup`/`notify` degrade to
  in-app + WARN interaction; nothing crashes.
- **Send failure** → draft `FAILED` + error_message; visible in app; "resend failed"
  re-queues.
- **Escalated (30+)** → no email generated; `flag_legal` + notify + Legal queue.
- **30+ flagged DISPUTED vs LEGAL** — old app used DISPUTED to keep it in the overdue
  list; here `flag_legal` sets LEGAL and the Legal queue surfaces it (no longer relies
  on the overdue filter). Decision: LEGAL + dedicated queue (cleaner).
- **Trigger loop** → scan writes `followup_queue`, run writes `invoices/drafts`;
  distinct tables prevent self-retrigger.
- **Empty states / single member** → seed guarantees a live first screen; roles
  collapse to one user gracefully.

---

## 15. Configurable knobs ("options all over")

- Per-pod (`pod_config`): auto_dispatch, human_in_loop, review_stage4,
  review_high_risk, email_enabled, notify_channel, company_name, sender_identity,
  approver/legal member ids.
- Per-invoice: tone override (force a stage tone), manual "Process now", "Regenerate".
- Per-scan: `force` (reprocess all), `failed_only` (resend failed).
- Risk weights: constants now; promotable to `pod_config` later.
- Channel per draft: EMAIL or WHATSAPP.

---

## 16. Hero moment, seed, success criteria

**Hero moment (≤60s, no narration):** open the app → overdue accounts already
triaged, drafts written and **grounded in real contract terms**, one stern notice
waiting in the finance-lead's approval inbox, a 30+ day account flagged LEGAL with
the legal team already pinged — and nobody clicked anything to make it happen.

**Seed (`seed/seed.sh`):** ~6 clients (mix of GOOD/AVERAGE/RISKY, one VIP) and
~12–15 invoices spanning every stage (reuse old sample data), a couple with prior
history, one parked in the approval inbox, one pre-flagged LEGAL; upload the
`/knowledge` policy + a sample contract. So the app opens alive.

**Success criteria (final smoke test):** run `daily-scan` → overdue invoices get
triaged, drafts created and policy-grounded → a HIGH-risk/stage-4 draft appears in
the finance-lead's approval inbox → approve it → marked sent + logged on the client
timeline → a 30+ day invoice is flagged LEGAL and the legal team notified → the app
shows the whole journey across role views, live.

---

## 17. Open decisions for you (pick or accept defaults)

1. **`drafts` as one table** (lifecycle incl. sent) — *default* — vs. split
   `drafts` + `messages`. Default keeps it simple for 2 days.
2. **Choreography** (`followup_queue` + DATASTORE fan-out) — *default, best demo* —
   vs. simpler app-button triggers if time is tight.
3. **Ring-2 priority:** which single wow first — **Gmail send+reply** (closes the
   loop) or **WhatsApp digest + legal ping** (most visual)? My lean: WhatsApp/Slack
   ping is the more screenshottable hero; Gmail reply is the deeper product story.
4. **Bounded retry:** fallback-only (*default*) vs. one agent retry then fallback.
5. **Notifications surface:** Slack/Telegram (no public webhook needed locally) vs.
   WhatsApp (most relatable for the demo).

---

## 18. Build order (full product, verified layer by layer)

We build bottom-up and verify each layer with the CLI before wiring the next
(pod-design testing strategy). No layer is skipped.

1. **Tables + `pod_config`** → import, `records create` smoke, `query run` checks.
2. **Seed data + `/knowledge` files** → upload, `stat` COMPLETED, search a phrase.
3. **Functions** (`classify_and_score`, `validate_draft`, `make_fallback_draft`,
   `dispatch_followup`, `notify`, `scan_overdue`, `metrics_snapshot`) → `functions run`
   each with realistic payloads; confirm grants + writes.
4. **Agents** (`collections-drafter`, then `reply-triager`, `digest-writer`) → `agents chat`,
   confirm output_schema conformance + RAG grounding.
5. **Workflow `collections-run`** → `workflows run`, drive both approval branches,
   inspect `step_history`.
6. **Schedules** (`daily-scan` TIME + `followup-dispatch` DATASTORE + digest/promise) →
   fire once, confirm, then pause test schedules.
7. **Connectors + surface** (Gmail, WhatsApp/Slack) → discovery loop, read-only smoke,
   then delegated end-to-end; record setup in README.
8. **App `collections-app`** → DESIGN.md, build, walk every scenario, deploy, view it.
9. **Whole thing** → run the success-criteria scenario against seed; confirm the hero
   moment is live on open.

Everything degrades gracefully without connectors, so the system is demoable from the
end of Phase 1 onward and only gets richer.
