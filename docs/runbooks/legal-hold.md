# Runbook: legal holds

A hold stops a deletion from **purging** while a legal or regulatory obligation applies. It does not stop the **freeze**: a request for a held subject makes it unreadable at once, exactly as any deletion would, and waits.

## Placing
Console: **Deletions → Legal holds → Place a hold…** (scope: a person or thing, an interaction, or the whole workspace; a reason code such as `litigation` or `regulator`; an optional note). API: `POST /v1/legal-holds {scope_kind, target_id, reason_code, note}` (admins; `governance.hold`). Placing a hold on a target whose deletion is already `purging` does not stop that run; place it before the request.

## What it does
- A deletion request whose closure touches a held entity or interaction (or any request while a workspace hold exists) is created `blocked_legal_hold`: frozen, nothing purged, a `deletion_exception` item `legal_hold` is open, and no stage job is queued.
- Releasing the hold (`DELETE /v1/legal-holds/{id}` or **Release…**) resolves the exception, moves the request back to `frozen` and queues the plan again. Another active hold that still covers the target keeps it blocked.
- The hold and its release are audited (`legal_hold.created`, `legal_hold.released`, `deletion.resumed`) and appear in the **Privacy audit** under Legal holds.

## Retaining on purpose
If only part of a request may not go (a memory under a hold), decide the `deletion_exception` with **Retain** and a reason; the receipt records it as kept and why.

## Checks
- `kg deletions list --workspace <ws> --status blocked_legal_hold`
- Console: Deletions → the request shows the hold banner.
