# How we used Kiro to build Claim Ledger

Track: Healthcare & Biotech (H4). Items marked TODO are not done yet and must be filled in or deleted before submission. Do not claim anything that was not actually done.

## 1. Which Kiro tools we used

- **Kiro IDE**: the main environment for the whole build (spec workflow, steering, hooks, chat sessions, task execution).
- TODO: Kiro CLI. If we try it, describe one real task we ran there and how it differed from the IDE. If we do not use it, delete this line.
- TODO: Kiro Web and Crew. Same rule: only describe them if used.

## 2. Spec-driven development

We used the requirements-first workflow and approved each stage before moving on.

- **Requirements (14).** Written in EARS style ("WHEN ... THE SYSTEM SHALL ..."). They cover gene resolution, Open Targets and PubMed retrieval, the evidence store and cache, claim construction, a verifier, unsupported-claim display, deterministic rendering, two conflict rules, wording and safety rules, offline resilience, testing strategy, a tamper demonstration, and accessibility.
- **We changed the spec before building.** After the first draft we asked Kiro to revise it five ways: define the Single_Source_Dependence conflict rule as a count of independent datatypes (the original "share of the score" idea had no formula); require the gene symbol returned to match the input instead of taking the first search hit; spell out the exact PubMed gene-plus-disease query and store PubMed's own query translation next to every count; name the offline example genes; and add a note that verification confirms a number matches its saved source, not that a finding is true. We later added the tamper demonstration, accessibility and export requirements, default thresholds, and a PubMed rate limit.
- **Design (about 700 lines).** Component diagram, data flow, data models, Evidence_ID scheme, the verifier and conflict rules, error handling, and 11 correctness properties mapped to requirements.
- **Tasks (19 top-level).** Bottom-up and test-driven, with dependency waves. We ran them in batches of a few tasks and ran the test suite after each.
- **A real mishap.** One round of requirement edits was shown in the editor but never written to disk. Kiro noticed the file was missing when we asked it to edit it, told us, and offered to regenerate it. We regenerated it with all changes included. The spec being on disk, not in chat, is what made the problem visible.
- TODO: spec versus vibe coding. Do one small task in plain vibe chat (for example a UI polish change), and compare honestly: what was faster, what was less predictable, what was harder to review.

## 3. Steering

We wrote one project steering file, `.kiro/steering/evidence-rules.md`, set to always load. It states ten rules, including: no claim without evidence; every number must equal the value recomputed from its evidence record; model output may only propose structured claims, never final prose; every raw API response is cached with its source, URL and retrieval time; careful wording ("associated with", never "causes"); and no recommendations.

The rule that made the biggest difference was **rule 8: write parsing code against a saved real response, never an assumed shape.** Kiro followed it structurally. The task list's second task builds fixture loaders over our committed real API responses before any parser exists, and the design has a section called "Grounding in real cached responses". The tests assert real quirks (for example PubMed returns counts as strings, and Open Targets datatype-score lists vary in length). Rule 9 (wording) became a render-time guard in the code and a property test.

## 4. Agent hooks

Two hooks in `.kiro/hooks/`:

1. **Run pytest on source and test saves.** When a file under `src/claimledger` or `tests` is saved, run the suite; if a test fails, fix the implementation, never weaken a test, and record any Hypothesis counterexample as a regression test.
2. **Scan for secrets when the agent stops.** Read-only scan of changed files for hardcoded keys; reports with the value masked.

Note: the first versions of the hook files were rejected by Kiro because they began with an invisible UTF-8 byte order mark added on Windows. Stripping it fixed the problem.

TODO: after the build, add one or two sentences on what each hook actually did or caught. If a hook never fired usefully, say so.

## 5. MCP

TODO. Not used yet. If we build the small MCP server (Open Targets and PubMed lookups, or exposing the verifier as a tool), describe what it enabled. If we do not build it, delete this section rather than inventing a story.

## 6. Property-based testing

We used Hypothesis with pytest for 11 properties, each tied to requirements: verifier soundness and completeness (checked against an independent oracle), no claim without evidence, rendering determinism, rendering provenance (unsupported claims never reach the dossier), conflict-rule symmetry and order independence, score recomputation fidelity, parse round-trip, the no-repair invariant, tamper rejection and reset, Evidence_ID stability, and wording/score-label safety.

- **A bug that example tests would likely have missed.** The Evidence_ID stability property failed on a Unicode input: the capital dotted I (U+0130) and its lowercase form produced two different IDs for one gene symbol, which would break cache and live parity. We had normalised symbols with `str.upper()`. Kiro fixed it with NFKC normalisation plus `casefold()` and kept the shrunk counterexample as a permanent regression test. Our example inputs were ASCII gene symbols, so ordinary unit tests would not have found it.
- **Honest scope.** The other properties found no counterexamples in the runs so far. Their value is as guards for future edits and as executable statements of the spec.
- **A disclosed test change.** In one task, Kiro narrowed the input generator for an existing property test so generated disease names could not contain forbidden standalone words. The renderer guard is tested separately by a positive test.

## 7. Kiro powers

TODO. Open the Powers panel in the IDE, install one that fits (for example a testing or Python one), use it for a real task, and describe what it changed. If we do not use any, say so plainly or delete this section.

## 8. Other notes

- 228+ tests pass at the last count (unit, example and property tests).
- Every parser was built against responses we saved from the real APIs before any code was written.
