# Implementation Plan: Claim Ledger

## Overview

This plan implements Claim Ledger incrementally, in the Python package `src/claimledger/` with tests under `tests/`, following the module layout and the single-invariant design: no number reaches the screen unless it is recomputed from a cached, real API response and matched exactly.

The build order is strictly bottom-up so every step wires into earlier ones with no orphaned code: scaffolding and config first; then (per STEERING RULE 8) fixture loaders built directly against the committed real responses in `data/cache` **before any parser is written**; then data models; cache/HTTP; evidence store with stable IDs; the two API clients with parsers tested against the fixtures; the shared `recompute.py` (single source of truth) built before/with the claim builder and verifier; the verifier and conflict detector with their property tests; the deterministic renderer with its wording guard; the exporter; the tamper/reset control; the pipeline wiring; the Streamlit UI with every mandatory notice/panel/legend/threshold/disambiguation/export/accessibility feature; the four Example_Genes offline cache (including selecting and fetching the fourth gene so it trips a conflict flag at default thresholds); and finally end-to-end offline validation.

Each correctness property from the design is implemented as its own property-based test sub-task (pytest + Hypothesis, `max_examples >= 100`), annotated with the design property number and the requirement clauses it validates.

## Tasks

- [x] 1. Scaffold package, config, and dependencies
  - [x] 1.1 Create package skeleton and confirm dependencies
    - Create `src/claimledger/__init__.py`, `src/claimledger/clients/__init__.py`, and empty module files for `config`, `models`, `cache`, `http`, `evidence`, `clients/opentargets`, `clients/pubmed`, `claims`, `recompute`, `verify`, `conflicts`, `render`, `export`, `pipeline`, `app` per the design Project Structure
    - Verify `requirements.txt` declares streamlit, requests (or httpx), pytest, and hypothesis; add any missing entry pinned to an exact version
    - Create `tests/conftest.py`, `tests/strategies.py`, and `tests/fixtures/cache/` placeholders
    - _Requirements: 12.1_

  - [x] 1.2 Implement Config and Thresholds dataclasses
    - Implement frozen `Thresholds` dataclass with defaults `top_n=5`, `retmax=3`, `support_threshold=0.3`, `min_supporting_datatypes=2`, `high_score_threshold=0.5`, `low_co_mention_threshold=100`
    - Implement frozen `Config` dataclass with `tool="claimledger"`, `email`, `ot_graphql_url`, `pubmed_esearch_url`, `http_timeout_s=10.0`, `max_requests_per_second=3.0`, and an Example_Gene registry (EGFR, TP53, BRCA1, plus a placeholder slot for the fourth gene filled in task 16)
    - _Requirements: 2.7, 3.6, 3.10, 9.6, 11.1_

  - [x]* 1.3 Write unit tests for config defaults
    - Assert every threshold and config default matches the specified values and that both dataclasses are frozen
    - _Requirements: 9.6, 2.7, 3.6_

- [x] 2. Build fixture loaders against committed real responses (STEERING RULE 8 — do this before any parser)
  - [x] 2.1 Implement cache-envelope fixture loaders in conftest
    - In `tests/conftest.py`, add fixtures that read the committed real responses `data/cache/preflight_ot_search.json`, `preflight_ot_associations.json`, `preflight_pubmed_gene.json`, and `preflight_pubmed_gene_and_disease.json` and expose their `{source_url, retrieved_at, payload}` envelopes
    - Assert the observed real shapes so later parsers are written against them: OT `associatedDiseases.count` is an **int**; PubMed `esearchresult.count`/`retmax`/`retstart` are **strings**; one OT row has 6 `datatypeScores` and another has 3 with differing order; `querytranslation` present on the gene+disease response
    - Provide a helper that builds an EvidenceStore from these envelopes for downstream tests
    - _Requirements: 4.6, 12.8_

  - [x] 2.2 Add a crafted malformed OT fixture for incomplete-row handling
    - Commit a small malformed OT associations fixture (a row missing an expected field) under `tests/fixtures/cache/` to drive the Requirement 2.6 incomplete-row path later
    - _Requirements: 2.6, 12.8_

- [x] 3. Implement core data models
  - [x] 3.1 Implement model dataclasses with explicit serialization
    - Implement frozen dataclasses `Source`, `EvidenceRecord` (incl. `submitted_query`, `query_translation`, `from_cache`), `DatatypeScore`, `Association` (incl. `incomplete`), `ClaimType`, `ClaimStatus`, `Claim` (incl. `evidence_ids` tuple, `status`, `reason`, `original_value`), `ConflictFlag`, and `VerificationResult`
    - Add explicit `to_dict`/`from_dict` on `Claim` (and nested `DatatypeScore`) for export and round-trip; no coercion/validation layer
    - _Requirements: 4.1, 4.2, 4.3, 5.1, 5.2, 6.4, 9.1_

  - [x]* 3.2 Write unit tests for model construction and serialization
    - Assert frozen-ness, value semantics/equality, and `to_dict`/`from_dict` symmetry for each claim-value type
    - _Requirements: 5.1, 5.2_

- [x] 4. Implement response cache and HTTP fetcher
  - [x] 4.1 Implement ResponseCache
    - Implement `ResponseCache` mapping a normalized request key to a cache file under `data/cache`, reading/writing the `{source_url, retrieved_at, payload}` envelope
    - _Requirements: 4.4, 11.7_

  - [x] 4.2 Implement RateLimiter and HttpFetcher with cache-first/fallback
    - Implement `RateLimiter` enforcing <= 3 req/s (min 1/3 s spacing) using a monotonic clock, injectable for tests
    - Implement `HttpFetcher.fetch(request_key, url, do_request)` with `http_timeout_s` timeout, cache-first for offline/Example_Gene mode, live-then-cache-write on success, cache fallback with `from_cache=True` on failure, and "interface unavailable" continuation when no cache exists
    - _Requirements: 3.9, 11.4, 11.5, 11.6_

  - [x]* 4.3 Write example-based tests for rate limiting and cache fallback
    - With a fake monotonic clock, assert >= 1/3 s spacing between successive PubMed calls
    - Assert that a simulated failure with cache present returns the cached envelope with `from_cache=True`, and absence yields the unavailable path
    - _Requirements: 3.9, 11.5, 11.6, 12.8_

- [x] 5. Implement Evidence store with stable Evidence_IDs
  - [x] 5.1 Implement EvidenceStore and normalized-request-key ID minting
    - Implement `EvidenceStore` with `add`, `get`, `contains`, keyed by `Evidence_ID`
    - Implement `evidence_id = source.value + ":" + sha256(normalized_request_key)[:16]` with per-source normalization (OT_SEARCH upper/trim symbol; OT_ASSOCIATIONS ensembl+top_n; PubMed canonicalized URL with sorted params, `tool`/`email` excluded from the hash)
    - Build an `EvidenceRecord` from a cache envelope mapping `source_url`→`url`, `retrieved_at`→`retrieved_at`, `payload`→`payload` (Req 11.7)
    - _Requirements: 4.1, 4.2, 4.3, 11.7_

  - [x] 5.2 Write property test for Evidence_ID stability
    - **Property 10: Evidence_ID stability**
    - **Validates: Requirements 4.2, 11.7**
    - Assert repeated generation yields the same id and that a cached-fetch key and a live-fetch key for the same logical request map to the same Evidence_ID

- [x] 6. Implement Open Targets client and parsers (against committed fixtures)
  - [x] 6.1 Implement OT search + resolution
    - Implement `OTClient.search(symbol)` parsing `payload.data.search.hits[]` -> `{id, name, entity}`, and `resolve(symbol, hits)` applying 1.3 exact case-insensitive name match, 1.5 no-target-hit stop, 1.6 ambiguous disambiguation (no auto-select), 1.7 chosen hit
    - _Requirements: 1.2, 1.3, 1.5, 1.6, 1.7_

  - [x] 6.2 Implement OT associations parser (parse datatype scores by id)
    - Implement `associated_diseases(ensembl_id)` parsing `data.target.{id, approvedSymbol, approvedName}`, `associatedDiseases.count` (int), and each row's `score`, `disease.{id,name}`, and `datatypeScores[]` keyed **by id, never by index**; mark `Association.incomplete=True` on a missing field and continue; select top `top_n` by descending score with `disease_id` tie-break
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

  - [x]* 6.3 Write example-based tests for OT parsing against the real fixtures
    - Parse `preflight_ot_search.json` (EGFR single hit resolves to ENSG00000146648) and `preflight_ot_associations.json`; assert int `count`, the 6-vs-3 variable-length datatype rows parsed correctly by id, and incomplete-row flagging via the crafted malformed fixture
    - _Requirements: 1.3, 2.3, 2.5, 2.6, 12.8_

- [x] 7. Implement PubMed client and parsers (against committed fixtures)
  - [x] 7.1 Implement PubMed esearch URL/param building and term formatting
    - Build esearch requests with `db=pubmed`, `retmode=json`, `retmax`, `term`, and `tool`+`email` on **every** request; format gene-only term `"GENE"[Title/Abstract]` and gene+disease term `"GENE"[Title/Abstract] AND "DISEASE NAME"[Title/Abstract]`
    - _Requirements: 3.1, 3.4, 3.6, 3.10_

  - [x] 7.2 Implement PubMed response parser
    - Parse `esearchresult.count` (string -> non-negative int), `idlist` (ordered PMIDs), `retmax`, `retstart`, and `querytranslation`; store the submitted query string and `querytranslation` for gene+disease; record count as **unavailable** (never fabricated) when absent/unparseable and continue; recent papers use `idlist` order
    - _Requirements: 3.2, 3.3, 3.5, 3.7, 3.8, 4.3_

  - [x]* 7.3 Write example-based tests for PubMed parsing against the real fixtures
    - Parse `preflight_pubmed_gene.json` and `preflight_pubmed_gene_and_disease.json`; assert string->int count conversion, stored `querytranslation` differing from the submitted term, count-unavailable handling, and that every built URL carries `tool` and `email`
    - _Requirements: 3.2, 3.5, 3.8, 3.10, 12.8_

- [x] 8. Implement the shared recompute functions (single source of truth)
  - [x] 8.1 Implement recompute_* pure functions
    - Implement `recompute_association`, `recompute_datatype_breakdown` (ordered by id), `recompute_literature_count`, `recompute_co_mention_count`, and `recompute_recent_paper` reading only from `EvidenceRecord` payloads; these are the sole numeric source shared by ClaimBuilder and Verifier
    - _Requirements: 4.5, 6.1_

  - [x] 8.2 Write property test for score recomputation fidelity
    - **Property 6: Score recomputation fidelity**
    - **Validates: Requirements 2.4, 2.5, 6.5**
    - Over associations with variable-length/shuffled datatype arrays, assert recompute reproduces Overall_Score and Datatype_Scores within 1e-9 by keying on id, not position

- [x] 9. Implement the Claim Builder
  - [x] 9.1 Implement ClaimBuilder for all five claim types
    - Build `ASSOCIATION`, `DATATYPE_BREAKDOWN`, `LITERATURE_COUNT`, `CO_MENTION_COUNT`, and `RECENT_PAPER` claims by calling the `recompute_*` functions, attaching >= 1 Evidence_ID to every claim; accept any model-proposed claims only as structured data; omit or emit-for-rejection (never silently keep) when a required value is unavailable
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8_

  - [x] 9.2 Write property test for no-claim-without-evidence
    - **Property 2: No claim without evidence**
    - **Validates: Requirements 5.2, 6.2**
    - Assert every built claim carries >= 1 Evidence_ID and no verifiable claim references a missing Evidence_ID

- [x] 10. Implement the Verifier
  - [x] 10.1 Implement recompute-and-compare verification with no repair
    - Implement `Verifier.verify(claim, store)`: reject on missing Evidence_ID (naming it); recompute via `recompute_*`; compare scores with 1e-9 absolute tolerance and counts/PMIDs by exact equality (datatype breakdown compared as a set keyed by id); accept on equality, reject on mismatch with a reason reporting both claim and recomputed values; never mutate/repair/substitute the claim value
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 7.1, 7.3, 7.4_

  - [x] 10.2 Write property test for verifier soundness and completeness
    - **Property 1: Verifier soundness and completeness**
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.6**
    - Assert accept iff every Evidence_ID present and value equals recompute (1e-9 for scores, exact for counts/PMIDs), with rejection reasons reporting both values or the missing id

  - [x] 10.3 Write property test for the no-repair invariant
    - **Property 8: No-repair invariant**
    - **Validates: Requirements 6.7, 13.4**
    - Snapshot the claim value before and after verify and assert it is identical for both Verified and Unsupported verdicts

- [x] 11. Checkpoint - core evidence/claim/verify pipeline
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Implement the Conflict Detector
  - [x] 12.1 Implement the two named, order-independent conflict rules
    - Implement `ConflictDetector.detect(association, thresholds)` for `Single_Source_Dependence` (flag when fewer than `min_supporting_datatypes` have score >= `support_threshold`; expose the counted supporting datatypes and count) and `Literature_Gap` (flag when `overall_score >= high_score_threshold` AND `co_mention_count <= low_co_mention_threshold`; skip when co-mention count unavailable); re-evaluable on any threshold change
    - _Requirements: 9.1, 9.2, 9.3, 9.5, 9.7, 9.8_

  - [x] 12.2 Write property test for conflict symmetry and order-independence
    - **Property 5: Conflict symmetry and order-independence**
    - **Validates: Requirements 9.2, 9.3**
    - Assert each rule's verdict is invariant to datatype-score ordering and to the order the two sides are supplied

- [x] 13. Implement the deterministic Dossier Renderer with wording guard
  - [x] 13.1 Implement template-based renderer and render-time wording guard
    - Render Markdown from Verified_Claims only, with no language model, sorted by the fixed composite `sort_key = (disease_rank, claim_type_rank, disease_id, claim_id)` and datatype scores sorted by id; use only "associated with"/"reported in"; label Overall_Score an evidence-strength score (never a probability); add a render-time guard asserting the output contains none of "causes", "proves", "validated", "promising"; never include Unsupported_Claims in prose
    - _Requirements: 7.2, 8.1, 8.2, 8.3, 8.4, 8.5, 10.1, 10.2, 10.5_

  - [x] 13.2 Write property test for rendering determinism
    - **Property 3: Rendering determinism**
    - **Validates: Requirements 8.2, 8.3**
    - Assert byte-identical output across repeated invocations and across every permutation of input ordering

  - [x] 13.3 Write property test for rendering provenance
    - **Property 4: Rendering provenance**
    - **Validates: Requirements 7.2, 8.1**
    - Assert no Unsupported_Claim contributes any content to rendered prose

  - [x] 13.4 Write property test for wording and score-label safety
    - **Property 11: Wording and score-label safety**
    - **Validates: Requirements 8.5, 10.1, 10.2, 10.5**
    - Assert prose uses only approved phrasings, contains none of the forbidden terms, and never presents a score as a probability

- [x] 14. Implement the Exporter and parse round-trip
  - [x] 14.1 Implement JSON + Markdown export
    - Serialize Verified_Claims and Unsupported_Claims to JSON and the dossier to Markdown using the models' explicit `to_dict`/`from_dict`
    - _Requirements: 14.5_

  - [x] 14.2 Write property test for parse round-trip
    - **Property 7: Parse round-trip**
    - **Validates: Requirements 12.6**
    - Assert claim to_dict/from_dict and payload parse -> serialize -> parse yield equivalent Claims

- [x] 15. Implement the Tamper / Reset mechanism
  - [x] 15.1 Implement tamper and reset operating only on Claim.value
    - Implement tamper that stores `original_value`, replaces `value`, and re-runs the Verifier (yielding Unsupported with both values in the reason); implement reset that restores `original_value` and re-runs (returning Verified); leave `EvidenceRecord` and cached response bytes unchanged
    - _Requirements: 13.1, 13.2, 13.3, 13.4_

  - [x] 15.2 Write property test for tamper rejection and reset
    - **Property 9: Tamper rejection and reset**
    - **Validates: Requirements 12.7, 13.1, 13.2, 13.3, 13.4**
    - Assert any distinct tamper value causes Unsupported (reason reports both values) and reset restores Verified, with no change to evidence or cache bytes

- [x] 16. Generate and validate the four Example_Genes offline cache
  - [x] 16.1 Select and fetch the fourth lesser-studied gene so it trips a conflict flag
    - Choose a lesser-studied Example_Gene and fetch/create its cached OT search, OT associations, PubMed gene, and PubMed gene+disease (per top disease) responses; validate against the Conflict_Detector at default thresholds (support 0.3, min datatypes 2, high score 0.5, low co-mention 100) that its cached data raises at least one Single_Source_Dependence or Literature_Gap flag; register it in the Example_Gene registry
    - _Requirements: 11.3_

  - [x] 16.2 Commit cached responses for all four Example_Genes
    - Commit EGFR, TP53, BRCA1, and the fourth gene cached responses under `data/cache` / `tests/fixtures/cache` so all four resolve, retrieve, build, verify, and render offline
    - _Requirements: 11.1, 11.2_

  - [x]* 16.3 Write example-based offline test for all four Example_Genes
    - Run the full pipeline offline for each Example_Gene from committed cache and assert a dossier renders and claims verify; assert the fourth gene raises at least one conflict flag at default thresholds; assert `from_cache=True` and the cache indication on the fallback path
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 12.8_

- [x] 17. Wire the Pipeline orchestrator
  - [x] 17.1 Implement Pipeline sequencing all layers
    - Implement `Pipeline` holding the per-run `EvidenceStore` and sequencing resolve -> OT associations -> PubMed gene + per-disease pairs (rate-limited, cache-first) -> build claims -> verify -> detect conflicts -> render, returning Verified_Claims, Unsupported_Claims, conflict flags, and the dossier
    - _Requirements: 1.1, 2.1, 3.1, 4.1, 5.1, 6.1, 8.1, 9.1, 11.4_

- [x] 18. Build the Streamlit UI
  - [x] 18.1 Implement gene input, validation, and disambiguation
    - Text input for a Gene_Symbol; empty/whitespace submit shows a validation message and issues no OT request; when no exact name match, render target hits as a selectable disambiguation list with no auto-select and drive resolution from the user's choice
    - _Requirements: 1.1, 1.4, 1.6, 1.7_

  - [x] 18.2 Implement the results screen with mandatory notices and the claim ledger
    - Persistent "Evidence summary, not medical advice" notice; the verification note (verification confirms a number matches saved evidence, not that the finding is true); claim ledger showing status as text (never color alone) with Unsupported claims listing type, Evidence_IDs, and reason; provenance view showing source/url/retrieved_at and, for co-mention counts, the submitted query and Query_Translation, plus a "came from cache" indicator
    - _Requirements: 4.7, 7.1, 7.4, 10.4, 10.6, 11.5, 14.3_

  - [x] 18.3 Implement threshold controls and side-by-side conflict panels
    - Display current threshold values with defaults (min_supporting_datatypes=2, support_threshold=0.3, high_score_threshold=0.5, low_co_mention_threshold=100) and re-run the Conflict_Detector on change; render each flagged association in a side-by-side panel named `Single_Source_Dependence`/`Literature_Gap`, showing counted datatype scores on one side and overall score vs co-mention count on the other, hiding neither side
    - _Requirements: 9.4, 9.5, 9.6, 9.7, 9.8_

  - [x] 18.4 Implement help panels, tamper/reset control, export control, and accessibility
    - "Why this matters" panel (who benefits, plain-language explanation, data sources Open Targets and PubMed); "How to read this" legend (Verified/Unsupported and the two conflict flags); tamper/reset control wired to task 15; download control exporting Verified + Unsupported claims as JSON and the dossier as Markdown; ensure status is text-conveyed with sufficient contrast
    - _Requirements: 13.1, 13.3, 14.1, 14.2, 14.3, 14.4, 14.5_

- [ ] 19. Final checkpoint and end-to-end offline validation
  - [ ] 19.1 Validate end-to-end offline operation across all layers
    - Run the full test suite; run the pipeline offline end-to-end for each of the four Example_Genes and confirm resolution, retrieval, claim construction, verification, conflict detection, rendering, and export all succeed from cache only
    - _Requirements: 11.4, 12.1_

  - [ ] 19.2 Checkpoint
    - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP; they are the unit, example-based, and property-based tests.
- Each correctness property (Properties 1–11) is implemented by exactly one property-based test sub-task, placed close to the implementation it protects, annotated with its property number and the requirement clauses it validates.
- STEERING RULE 8 is honored by task 2 building fixture loaders against the committed real responses in `data/cache` before any parser (tasks 6 and 7) is written.
- `recompute.py` (task 8) is built before the ClaimBuilder (task 9) and Verifier (task 10) so both share one numeric source of truth.
- The fourth Example_Gene (task 16.1) must be selected and its cache fetched/created, then validated to trip at least one conflict flag at the default thresholds before it is registered.
- Checkpoints (tasks 11 and 19.2) ensure incremental validation.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.3"] },
    { "id": 1, "tasks": ["2.1", "2.2", "3.1", "3.2", "4.1"] },
    { "id": 2, "tasks": ["4.2", "5.1", "8.1"] },
    { "id": 3, "tasks": ["4.3", "5.2", "6.1", "6.2", "7.1", "7.2", "8.2"] },
    { "id": 4, "tasks": ["6.3", "7.3", "9.1", "12.1"] },
    { "id": 5, "tasks": ["9.2", "10.1", "12.2"] },
    { "id": 6, "tasks": ["10.2", "10.3", "13.1", "14.1", "15.1"] },
    { "id": 7, "tasks": ["13.2", "13.3", "13.4", "14.2", "15.2", "16.1"] },
    { "id": 8, "tasks": ["16.2", "17.1"] },
    { "id": 9, "tasks": ["16.3", "18.1", "18.2", "18.3", "18.4"] },
    { "id": 10, "tasks": ["19.1"] }
  ]
}
```
