---
title: Incident Postmortem Process
category: runbook
owner: analytics-platform-team
tags: [incident, postmortem, review, blameless, process]
last_reviewed: 2026-11-02
---

# Incident Postmortem Process

Every Sev 1 and Sev 2 incident gets a written postmortem within five business days.

## What a postmortem must contain

| Section | Purpose |
|---|---|
| Timeline | What happened, in UTC, from first signal to resolution |
| Customer impact | Which dashboards or consumers were affected, and for how long |
| Root cause | The technical cause, not the person who triggered it |
| Detection gap | Why the alarm fired late, or did not fire |
| Action items | Owned, dated, and tracked outside the document |

Action items live in the tracker rather than the document, because items recorded only in
prose are never revisited once the incident stops being memorable.

## Blameless means specific

Blameless does not mean vague. Naming that a deploy at 14:02 removed a null check is
required; naming who wrote it is not. Teams that soften the technical detail to avoid
implying blame end up with postmortems nobody can learn from.

## Review cadence

Open action items are reviewed at the start of each sprint. An item older than two sprints
is escalated to the platform lead, since an action item that survives two sprints is
usually mis-scoped rather than deprioritised.
