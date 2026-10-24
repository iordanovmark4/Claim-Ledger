---
inclusion: always
---
# Evidence rules for Claim Ledger (project-specific, non-negotiable)

1. No claim without evidence. Every claim has at least one evidence_id.
2. Every evidence_id must exist in the evidence store for the current run.
3. Every number in a claim must equal the value recomputed from its evidence record. Reject mismatches. Never repair them.
4. Claims are structured data first. Prose is rendered only from Verified claims, deterministically.
5. Any model output may only propose claims as JSON. It never writes final prose.
6. Unsupported claims are shown as Unsupported with a reason, or dropped. Never silently kept.
7. Conflicting evidence is shown side by side. Never resolved by hiding one side.
8. Cache every raw API response in data/cache with source, URL and retrieved_at. Write parsing code against a saved real response, never an assumed shape.
9. Wording: say "associated with" or "reported in". Never say "causes", "proves", "validated", or "promising". An Open Targets score is an evidence-strength score, not a probability.
10. The app never recommends a drug, target or treatment, and never gives medical advice.
