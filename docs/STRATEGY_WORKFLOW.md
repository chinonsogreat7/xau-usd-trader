# Structured strategy-learning workflow

Status: diagnostic research boundary; no broker or P&L execution

## Purpose

The video-learning path converts authorized evidence into inert JSON data, not generated Python.
Natural-language output cannot directly become a strategy, backtest, order, or promotion decision.

```text
authorized source + transcript
            |
            v
unfrozen JSON draft from extractor
            |
            v
strict schema + evidence validation ----> reject / unresolved review queue
            |
            v
human-reviewed frozen revision
            |
            v
hash-bound diagnostic compiled plan
```

## Current gates

1. `ClosedLearningPipeline` requires one matching source ID and content hash, matching source
   identity metadata, video evidence intervals that overlap the authorized transcript, and an
   `excerpt_sha256` recomputed from the canonical overlapping transcript-segment payload.
2. `validate_strategy_spec_v1` rejects unknown fields, implicit type coercion, arbitrary rule text,
   unsupported operators/references, missing evidence coverage, feature cycles, understated
   warm-up, inconsistent sizing/risk, invalid schedules, and non-paper execution settings.
3. The extractor may return only an unfrozen draft. It cannot approve or freeze its own output.
4. Canonical JSON produces `spec_hash`; compilation revalidates the canonical document so forged
   readiness flags cannot bypass the gate.
5. `compile_strategy_spec_v1` accepts only a validated, frozen, resolved v1 artifact. It lowers
   rules into immutable operands and condition nodes and binds the spec, compiler, and feature
   manifest hashes into `plan_hash`.
6. The evaluator uses fixed local decimal arithmetic, strong tri-state logic, prefix-only data,
   contiguous feature/confirmation history, explicit availability, decision-time-bound position
   context, UTC entry windows, exit-before-entry ordering, and a conflict result when long and
   short are both true.

## Deliberate limitations

- The plan is `diagnostic_only=true`. It is not accepted by the current P&L backtester or a broker.
- Stop/target fills, trailing stops, fixed-fractional quantity, quote TTL, equal-timestamp event
  order, cooldown/holding state transitions, `close_on_data_stale`, and account-wide risk require
  the future observed-clock event-driven simulator. The stale-close policy is hash-bound but never
  executed by the current evaluator, which has no quote-age observation event to timestamp safely.
- Evidence excerpt hashes are recomputed from canonical overlapping transcript segments. Persisting
  that transcript and evidence index as an independently reviewed, versioned artifact is still a
  future ingestion milestone.
- The pipeline does not download videos or bypass platform controls. Use owned, licensed,
  creator-provided, user-supplied, or otherwise expressly permitted material.
- A valid or compiled strategy is still only a falsifiable hypothesis, not evidence of profitability.

## Commands

Input must be strict JSON. Duplicate object keys and non-finite numbers are rejected at parsing.

```bash
PYTHONPATH=src python3 -m xau_trader validate-strategy strategy.json
PYTHONPATH=src python3 -m xau_trader validate-strategy strategy.json --promotable
PYTHONPATH=src python3 -m xau_trader compile-strategy strategy.json
```

`validate-strategy` prints readiness, warnings, and `spec_hash`. `compile-strategy` requires a
promotion-ready frozen document and prints only diagnostic plan metadata, including `plan_hash` and
the feature-manifest hash.
