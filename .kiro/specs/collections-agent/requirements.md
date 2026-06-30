# Requirements — Collections Agent

## Introduction

An AI accounts-receivable follow-up agent built as a Lemma pod. It tracks clients and
their overdue invoices, classifies each invoice into an escalation stage, drafts a
stage-appropriate follow-up grounded in the team's real policy/contract documents,
validates that draft against the source data to prevent hallucination, scores risk
deterministically, and either auto-sends low-risk follow-ups or routes high-risk /
final notices to a human for approval. A team of roles (collector, finance-lead, legal,
admin) works the same shared state through an operator app, with a full audit trail and
multi-channel notifications (email out + in, WhatsApp/Slack for digests, legal
escalations, and strict customer notices).

This rebuilds and improves on a prior FastAPI/LangGraph/Celery/Next.js app, collapsing
~10 external integrations into the Lemma platform and fixing its data-model and
single-user limitations.

Glossary: **stage** = escalation level derived from days overdue; **risk** =
deterministic priority signal; **draft** = a generated follow-up message;
**interaction** = an immutable timeline/audit event.

---

## Requirement 1 — Client & invoice data model

**User story:** As a collector, I want clients and their invoices stored relationally
with per-client history, so I can see who owes what and everything we've done about it.

#### Acceptance criteria
1. WHEN an invoice is created THEN it SHALL reference exactly one `clients` row via `client_id`.
2. WHEN invoice data is imported with a client that does not exist THEN the system SHALL create or link the client by `(name, email)`.
3. The `clients` table SHALL store `name, email, phone, company, vip, payment_behavior (GOOD/AVERAGE/RISKY), owner, notes`.
4. The `invoices` table SHALL store `invoice_no, client_id, amount, currency, due_date, days_overdue, stage, status, risk_level, payment_link, followup_count, last_followup_at, last_processed_at, notes`.
5. All domain tables SHALL be shared (`enable_rls: false`) so the whole team sees the same data.
6. WHEN an invoice `amount` is ≤ 0 or exceeds 10,000,000 THEN the create/import SHALL be rejected with a clear error.
7. The system SHALL NOT trust an imported `days_overdue`; it SHALL recompute it from `due_date`.

## Requirement 2 — Escalation stage classification

**User story:** As a collector, I want each invoice automatically classified by how
overdue it is, so the right tone and policy apply without manual sorting.

#### Acceptance criteria
1. WHEN `classify_and_score` runs for an invoice THEN it SHALL recompute `days_overdue` from `due_date` and today.
2. The system SHALL map days overdue to stage: ≤0 `PENDING`, 1–7 `STAGE_1`, 8–14 `STAGE_2`, 15–21 `STAGE_3`, 22–30 `STAGE_4`, 30+ `ESCALATED`.
3. IF stage is `ESCALATED` THEN the system SHALL NOT generate an email and SHALL route to legal flagging.
4. IF invoice status is not `ACTIVE` (PAID/DISPUTED/LEGAL/PAUSED) THEN the invoice SHALL be skipped by the scan.

## Requirement 3 — Deterministic risk scoring

**User story:** As a finance-lead, I want a trustworthy risk signal (not an LLM guess),
so human review is triggered by real exposure.

#### Acceptance criteria
1. WHEN `classify_and_score` runs THEN it SHALL compute `risk_level` (LOW/MEDIUM/HIGH) from amount, days overdue, and client `payment_behavior`, with a VIP adjustment.
2. The risk computation SHALL be deterministic and explainable (same inputs → same output).
3. The system SHALL persist `risk_level` on the invoice and the draft.

## Requirement 4 — AI follow-up drafting grounded in policy

**User story:** As a collector, I want professionally written, stage-appropriate
follow-ups that cite our actual terms, so I don't write them by hand and they're accurate.

#### Acceptance criteria
1. WHEN drafting THEN `collections-drafter` SHALL produce a structured `GeneratedEmail` (subject, body, tone, escalation_stage, confidence, echoed source fields, reasoning, policy_citations).
2. The agent SHALL search `/knowledge` and ground the email in retrieved policy/contract terms.
3. The agent SHALL set tone per stage: STAGE_1 warm, STAGE_2 polite-firm, STAGE_3 formal-serious, STAGE_4 stern-urgent.
4. The agent SHALL only use invoice/client values provided; it SHALL echo `invoice_id_used, client_name_used, amount_used, days_overdue_used` for verification.
5. IF a tone override is supplied THEN the agent SHALL honor it regardless of stage.

## Requirement 5 — Anti-hallucination validation

**User story:** As a finance-lead, I want every draft verified against the source record
in code, so we never send a fabricated amount, name, or invoice number.

#### Acceptance criteria
1. WHEN a draft is generated THEN `validate_draft` SHALL cross-check the four echoed fields against the source invoice (invoice_no exact, client_name case-insensitive, amount ±0.01, days_overdue ±1).
2. WHEN the body contains a placeholder (`[INSERT`, `TBD`, `PLACEHOLDER`, `{{`, `XXX`) THEN validation SHALL fail.
3. WHEN any client-supplied text contains prompt-injection patterns THEN it SHALL be sanitized before drafting.
4. IF validation fails THEN the system SHALL produce a deterministic fallback draft and route it to human review (never auto-send).
5. The system SHALL record validation outcome and errors on the draft and in `interactions`.

## Requirement 6 — Routing: auto-send vs human approval

**User story:** As a finance-lead, I want low-risk follow-ups sent automatically and
risky/final ones held for my approval, so I spend attention only where it matters.

#### Acceptance criteria
1. WHEN a validated draft has LOW/MEDIUM risk, stage < 4, and `auto_dispatch` is on THEN the system SHALL auto-send it.
2. WHEN risk is HIGH OR stage is `STAGE_4` OR `auto_dispatch` is off THEN the system SHALL route the draft to a finance-lead approval FORM.
3. WHEN the approver approves THEN the (optionally edited) draft SHALL be sent and logged as human-approved.
4. WHEN the approver rejects THEN no message SHALL be sent and the draft SHALL be marked rejected with the reason.
5. WHEN the approver flags legal THEN the invoice status SHALL become `LEGAL` and legal SHALL be notified.
6. WHEN a new draft is generated for an invoice with a pending-review draft THEN the older draft SHALL be superseded (no duplicates in the queue).

## Requirement 7 — Dispatch & delivery tracking

**User story:** As a collector, I want every send attempt recorded with its outcome,
so I can see exactly what went to each client and resend failures.

#### Acceptance criteria
1. WHEN a follow-up is dispatched AND `email_enabled` is on with Gmail connected THEN it SHALL send via Gmail; ELSE it SHALL be recorded as an in-app send.
2. WHEN a send succeeds THEN `followup_count` and `last_followup_at` SHALL be updated; WHEN it fails THEN the draft SHALL be marked `FAILED` with an error and not increment the count.
3. Every dispatch attempt (success or failure) SHALL write an `interactions` row.
4. The system SHALL expose a way to re-queue invoices whose last send failed.

## Requirement 8 — Legal escalation

**User story:** As a legal team member, I want 30+ day and flagged accounts routed to me
with notification, so nothing slips into limbo.

#### Acceptance criteria
1. WHEN an invoice reaches `ESCALATED` (30+ days) THEN the system SHALL set status `LEGAL`, log `LEGAL_FLAGGED`, and notify legal.
2. The app SHALL provide a Legal queue showing all `LEGAL` invoices with context.
3. WHEN `notify_channel` is configured THEN legal escalation SHALL send a notification on that channel; ELSE it SHALL log the notification as not-delivered (WARN) without failing the flow.

## Requirement 9 — Team roles & shared visibility

**User story:** As a team, we want multiple roles working the same data with appropriate
views, so it's clearly a shared operation, not one person's spreadsheet.

#### Acceptance criteria
1. The system SHALL support roles collector, finance-lead, legal, admin via Lemma member roles.
2. The app SHALL present role-relevant views (collector queue, finance-lead approvals, legal queue, admin settings) over shared tables.
3. The approval FORM SHALL be assignable to a configured finance-lead member.
4. WHEN only one member exists THEN all role views SHALL still function (roles collapse to that member).

## Requirement 10 — Audit trail & history

**User story:** As any team member, I want a complete, immutable timeline per invoice and
per client, so we have full accountability.

#### Acceptance criteria
1. Every significant action (ingest, draft, send, reply, status change, escalation, approval, rejection, fallback, notification) SHALL append an `interactions` row.
2. The app SHALL show a chronological timeline for an invoice and for a client.
3. `interactions` rows SHALL record actor (user or workload label), channel, direction, and level.

## Requirement 11 — Scheduled scanning & reactive processing

**User story:** As a collector, I want overdue invoices processed automatically every day,
so follow-ups go out without me triggering them.

#### Acceptance criteria
1. WHEN the daily scan fires THEN `scan_overdue` SHALL recompute all ACTIVE invoices and enqueue those needing follow-up (overdue, not paid/paused/legal, not already processed today unless forced).
2. WHEN an invoice is enqueued THEN a reactive trigger SHALL start `collections-run` for it.
3. The system SHALL avoid trigger loops (scan writes a different table than the run).
4. The system SHALL provide a manual "Run scan" and per-invoice "Process now" action.

## Requirement 12 — Operator app

**User story:** As an operator, I want a polished, live UI with everything (dashboard,
queue, client detail, approvals, legal, sent log, audit, settings), so I run collections
from one place.

#### Acceptance criteria
1. The app SHALL provide Dashboard, Invoices, Client detail, Approvals, Legal queue, Sent log, Audit, and Settings views.
2. The app SHALL update live on record changes via WebSocket (no polling).
3. The app SHALL provide designed loading, empty, error, and permission states.
4. The app SHALL allow per-invoice tone override, regenerate, and manual process.
5. The app SHALL read pod context from injected runtime config (no hardcoded host/pod id) so it runs unchanged local or cloud.

## Requirement 13 — Runtime configuration (Settings)

**User story:** As an admin, I want to toggle behavior at runtime, so I can switch
between safe demo mode and live sending without code changes.

#### Acceptance criteria
1. The system SHALL store runtime config in `pod_config`: auto_dispatch, human_in_loop, review_stage4, review_high_risk, email_enabled, notify_channel, company_name, sender_identity, approver/legal member ids.
2. WHEN a config value changes THEN subsequent runs SHALL respect it.
3. The Settings view SHALL surface and edit these values.

## Requirement 14 — Inbound reply handling (loop closed)

**User story:** As a collector, I want client replies understood and acted on, so a
"paid"/"dispute"/"promise" reply updates state automatically.

#### Acceptance criteria
1. WHEN an inbound reply arrives via the connected mailbox THEN `reply-triager` SHALL classify it (PROMISE_TO_PAY/DISPUTE/PAID_CLAIM/QUESTION/OTHER) and extract any promised date/amount.
2. WHEN classified PAID_CLAIM THEN the invoice SHALL be marked for verification; WHEN DISPUTE THEN status `DISPUTED` + notify; WHEN PROMISE_TO_PAY THEN a `promises` row SHALL be created.
3. Every inbound reply SHALL be logged as an `interactions` row (direction INBOUND).

## Requirement 15 — Notifications & surfaces

**User story:** As a team, we want WhatsApp/Slack notifications for daily stats and legal
escalations, and the ability to send strict notices, so we stay informed off-app.

#### Acceptance criteria
1. WHEN the daily-digest schedule fires THEN the system SHALL compute stats and post a digest to the configured channel.
2. WHEN an invoice is flagged legal THEN the system SHALL ping the legal channel.
3. The system SHALL support sending a strict notice to a client via the configured channel.
4. IF no channel is configured THEN notification attempts SHALL be logged (not delivered) without breaking any flow.

## Requirement 16 — Promise-to-pay tracking

**User story:** As a collector, I want promises tracked and re-checked, so broken promises
re-escalate automatically.

#### Acceptance criteria
1. WHEN a promise is recorded THEN it SHALL store `promised_date`, optional `promised_amount`, and status `OPEN`.
2. WHEN a promise's date passes and the invoice is still unpaid THEN the promise SHALL be marked `BROKEN` and the invoice re-queued for follow-up.

## Requirement 17 — Graceful degradation & robustness

**User story:** As a builder/demoer, I want the system to work fully with zero external
connectors, so the core never breaks during a demo.

#### Acceptance criteria
1. WHEN no connectors are configured THEN drafting, validation, routing, approval, and in-app dispatch SHALL all work.
2. WHEN a connector call fails or is unconfigured THEN the system SHALL degrade to in-app behavior and log a WARN interaction.
3. WHEN a workflow node fails THEN the failure SHALL be visible (run history + `followup_queue` ERROR) and re-runnable.

## Requirement 18 — Seed & demo readiness

**User story:** As a demoer, I want the pod to open alive with realistic data and a clear
hero moment, so it's instantly compelling.

#### Acceptance criteria
1. The seed SHALL create clients and invoices spanning every stage, with at least one parked in approval and one flagged legal, plus uploaded `/knowledge` documents.
2. WHEN the app opens on seeded data THEN it SHALL show triaged accounts, drafts, a pending approval, and a legal escalation without any manual action.
3. The seed SHALL be reproducible via a `seed/seed.sh` script recorded in the README.
