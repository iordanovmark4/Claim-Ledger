# Requirements Document

## Introduction

Claim Ledger is a Streamlit web application (Python 3.10+) that builds an auditable, evidence-backed dossier about a human gene and its reported disease associations. A user enters a gene symbol; the application resolves the symbol to an Ensembl identifier through the Open Targets GraphQL search, retrieves disease-association evidence from the Open Targets GraphQL API, and retrieves literature evidence from the PubMed E-utilities esearch endpoint.

Every retrieved API result is persisted as an evidence record and its raw response is cached on disk. The application then constructs structured claims of five fixed types, and a verifier accepts a claim only when its evidence exists in the store and every number in the claim equals the value recomputed from that evidence. Dossier prose is rendered deterministically from verified claims only; no language model is required at runtime. The application surfaces two named, symmetric conflict rules with user-visible, configurable thresholds, and it operates from cached responses when an external interface is unavailable so that at least three preloaded example genes (EGFR, TP53, and BRCA1, plus one additional lesser-studied gene) always work offline.

All behavior in this document is subordinate to the project steering rules in .kiro/steering/evidence-rules.md, which are non-negotiable.

## Glossary

- Claim_Ledger: The Streamlit web application described by this document.
- Gene_Resolver: The component that resolves a user-entered gene symbol to an Ensembl identifier using Open Targets search.
- OT_Client: The component that retrieves disease-association evidence from the Open Targets GraphQL API.
- PubMed_Client: The component that retrieves literature evidence from the PubMed E-utilities esearch endpoint.
- Evidence_Store: The in-run collection of evidence records keyed by stable evidence identifier.
- Response_Cache: The on-disk cache of raw API responses located in data/cache.
- Claim_Builder: The component that constructs structured claims from evidence records.
- Verifier: The component that accepts or rejects each claim.
- Dossier_Renderer: The component that renders dossier prose deterministically from verified claims.
- Conflict_Detector: The component that evaluates the two named conflict rules.
- Tamper_Control: The user-interface control that deliberately alters the displayed value of one selected Claim and re-runs the Verifier on it, together with the reset control that restores the original value.
- Test_Suite: The pytest and Hypothesis test collection for the application.
- Gene_Symbol: A user-entered human gene symbol string (for example, EGFR).
- Ensembl_ID: An Open Targets target identifier of the form ENSG followed by digits (for example, ENSG00000146648).
- Evidence_Record: A stored record with fields evidence_id, source, url, retrieved_at, and payload (the raw response body); for a gene-plus-disease PubMed response the record also stores the exact submitted query string and the esearchresult.querytranslation returned by PubMed.
- Evidence_ID: A stable string identifier for an Evidence_Record, unique within a run.
- Overall_Score: The Open Targets association score, a float in the range 0 through 1 inclusive, presented as an evidence-strength score and never as a probability.
- Datatype_Score: An Open Targets per-datatype score object with fields id and score, where id is one of clinical, genetic_literature, affected_pathway, literature, genetic_association, or somatic_mutation.
- Literature_Count: The integer value parsed from the PubMed esearchresult.count string for a gene-only query.
- Co_Mention_Count: The integer value parsed from the PubMed esearchresult.count string for a gene-plus-disease query.
- Query_Translation: The value of esearchresult.querytranslation returned by PubMed, recording how PubMed interpreted the submitted query string.
- PMID: A PubMed identifier string from esearchresult.idlist.
- Claim: A structured record of one of five fixed types, carrying a value and one or more Evidence_IDs.
- Verified_Claim: A Claim the Verifier has accepted.
- Verified_Claims: The collection of all Claims the Verifier has accepted in a run.
- Unsupported_Claim: A Claim the Verifier has rejected, retained with a human-readable reason.
- Unsupported_Claims: The collection of all Claims the Verifier has rejected in a run.
- Support_Threshold: A configurable Datatype_Score value, defaulting to 0.3, at or above which a datatype is counted as a supporting datatype for the Single_Source_Dependence rule.
- Minimum_Supporting_Datatype_Count: A configurable integer N, defaulting to 2, giving the minimum number of datatypes that must reach the Support_Threshold for an association to avoid a Single_Source_Dependence flag.
- Single_Source_Dependence: The conflict rule for an association supported by too few datatypes, flagged when fewer than Minimum_Supporting_Datatype_Count datatypes have a Datatype_Score at or above the Support_Threshold.
- Literature_Gap: The conflict rule for an association with a high Overall_Score but a very low Co_Mention_Count.
- High_Score_Threshold: A configurable Overall_Score value, defaulting to 0.5, at or above which an association is considered high-scoring for the Literature_Gap rule.
- Low_Co_Mention_Threshold: A configurable integer, defaulting to 100, at or below which a Co_Mention_Count is considered very low for the Literature_Gap rule.
- Example_Gene: One of at least three preloaded gene symbols whose cached responses are bundled so the application works offline; the set includes EGFR, TP53, and BRCA1 and at least one additional lesser-studied gene.

## Requirements

### Requirement 1: Gene Input and Resolution

**User Story:** As a researcher, I want to enter a gene symbol and have the application resolve it to an Ensembl identifier, so that I can retrieve evidence for a specific target.

#### Acceptance Criteria

1. THE Claim_Ledger SHALL present a text input that accepts a Gene_Symbol.
2. WHEN a user submits a non-empty Gene_Symbol, THE Gene_Resolver SHALL query the Open Targets GraphQL search endpoint at https://api.platform.opentargets.org/api/v4/graphql for target entities matching the Gene_Symbol.
3. WHEN the Open Targets search response contains at least one hit under data.search.hits with entity equal to target whose name equals the submitted Gene_Symbol compared case-insensitively, THE Gene_Resolver SHALL select that hit and extract its id as the Ensembl_ID and its name as the resolved symbol.
4. IF the submitted Gene_Symbol is empty or contains only whitespace, THEN THE Claim_Ledger SHALL display a validation message requesting a non-empty Gene_Symbol and SHALL NOT issue an Open Targets request.
5. IF the Open Targets search response contains no hit with entity equal to target, THEN THE Claim_Ledger SHALL display a message that the Gene_Symbol could not be resolved and SHALL stop the retrieval flow for that submission.
6. IF the Open Targets search response contains at least one hit with entity equal to target but no such hit has a name that equals the submitted Gene_Symbol compared case-insensitively, THEN THE Claim_Ledger SHALL display the target hits and SHALL ask the user to choose which hit to use rather than auto-selecting the first hit.
7. WHEN the user chooses a target hit from the displayed disambiguation list, THE Gene_Resolver SHALL extract the chosen hit's id as the Ensembl_ID and its name as the resolved symbol.

### Requirement 2: Open Targets Disease-Association Retrieval

**User Story:** As a researcher, I want the application to retrieve disease associations for the resolved target, so that I can see which diseases are reported in association with the gene and how strong that evidence is.

#### Acceptance Criteria

1. WHEN a Gene_Symbol has been resolved to an Ensembl_ID, THE OT_Client SHALL request the target's associated diseases from the Open Targets GraphQL API using the Ensembl_ID.
2. WHEN the Open Targets association response is received, THE OT_Client SHALL parse data.target.id, data.target.approvedSymbol, and data.target.approvedName.
3. WHEN the Open Targets association response is received, THE OT_Client SHALL parse data.target.associatedDiseases.count as the total association count and SHALL parse each entry of data.target.associatedDiseases.rows.
4. WHEN parsing an association row, THE OT_Client SHALL extract the score as the Overall_Score, the disease.id and disease.name, and each datatypeScores entry as a Datatype_Score with its id and score.
5. WHERE a datatypeScores array varies in length or ordering across association rows, THE OT_Client SHALL parse each array by element content and SHALL NOT assume a fixed length, order, or set of datatype identifiers.
6. IF an expected field is absent from an association row, THEN THE OT_Client SHALL record the row as incomplete and SHALL continue parsing the remaining rows.
7. THE OT_Client SHALL select a configurable number of top-scoring associated diseases, defaulting to five, ordered by descending Overall_Score, for downstream claim construction and literature queries.

### Requirement 3: PubMed Literature Retrieval

**User Story:** As a researcher, I want the application to retrieve PubMed paper counts and recent papers for the gene and for the gene paired with top diseases, so that I can gauge literature support.

#### Acceptance Criteria

1. WHEN a Gene_Symbol has been resolved, THE PubMed_Client SHALL query the PubMed E-utilities esearch endpoint for papers matching the gene and SHALL request JSON output.
2. WHEN a PubMed esearch response is received, THE PubMed_Client SHALL parse esearchresult.count as a string and SHALL convert that string to a non-negative integer paper count.
3. WHEN a PubMed esearch response is received, THE PubMed_Client SHALL parse esearchresult.idlist as the ordered list of PMID strings and SHALL parse esearchresult.retmax and esearchresult.retstart.
4. FOR each selected top disease, THE PubMed_Client SHALL issue a gene-plus-disease esearch query whose term is formatted as "GENE"[Title/Abstract] AND "DISEASE NAME"[Title/Abstract], with the gene symbol and the disease name each enclosed in double quotes, each followed by the [Title/Abstract] field tag, and the two clauses joined by AND.
5. WHEN a gene-plus-disease esearch response is received, THE PubMed_Client SHALL parse its esearchresult.count as the Co_Mention_Count for that gene-disease pair and SHALL parse its esearchresult.querytranslation as the Query_Translation for that query.
6. THE PubMed_Client SHALL request at most a configurable number of recent PMIDs per query, defaulting to three, through the esearch retmax parameter.
7. WHEN selecting recent papers for display, THE PubMed_Client SHALL use the PMIDs in the order returned in esearchresult.idlist.
8. IF esearchresult.count is absent or cannot be converted to a non-negative integer, THEN THE PubMed_Client SHALL record the affected count as unavailable and SHALL continue processing the remaining queries.
9. THE PubMed_Client SHALL rate-limit requests to the PubMed E-utilities endpoint to at most three requests per second.
10. THE PubMed_Client SHALL include a tool parameter and an email parameter on every PubMed E-utilities request.

### Requirement 4: Evidence Store and Response Caching

**User Story:** As a researcher, I want every API result stored as a traceable evidence record with its raw response cached, so that every number can be traced back to a real source.

#### Acceptance Criteria

1. WHEN any API response is received from Open Targets or PubMed, THE Evidence_Store SHALL create an Evidence_Record containing an Evidence_ID, the source, the request url, the retrieved_at timestamp, and the raw response payload.
2. THE Evidence_Store SHALL assign each Evidence_Record an Evidence_ID that is unique within the current run and stable for the lifetime of that run.
3. WHEN an Evidence_Record is created for a gene-plus-disease PubMed response, THE Evidence_Store SHALL store in that Evidence_Record the exact submitted query string and the esearchresult.querytranslation returned by PubMed.
4. WHEN an Evidence_Record is created, THE Response_Cache SHALL persist the raw response under data/cache together with its source, url, and retrieved_at.
5. THE Claim_Builder and the Verifier SHALL recompute claim values only from Evidence_Records held in the Evidence_Store and SHALL NOT use values from any other source.
6. WHERE parsing logic reads an API response, THE Claim_Ledger SHALL parse against the saved real response shape as captured in data/cache and SHALL NOT rely on an assumed response shape.
7. WHEN a user requests the source for a displayed number, THE Claim_Ledger SHALL show the source, url, and retrieved_at of the Evidence_Record referenced by the corresponding Claim, and for a Co_Mention_Count SHALL also show the exact submitted query string and the Query_Translation beside the count.

### Requirement 5: Claim Construction

**User Story:** As a researcher, I want the application to turn evidence into structured claims of fixed types, so that each displayed statement is backed by data rather than free text.

#### Acceptance Criteria

1. THE Claim_Builder SHALL construct Claims of exactly these five types: ASSOCIATION, DATATYPE_BREAKDOWN, LITERATURE_COUNT, CO_MENTION_COUNT, and RECENT_PAPER.
2. THE Claim_Builder SHALL attach at least one Evidence_ID to every Claim it constructs.
3. WHEN constructing an ASSOCIATION Claim for a gene-disease pair, THE Claim_Builder SHALL set its value to the Overall_Score from the Open Targets association Evidence_Record and SHALL reference that Evidence_ID.
4. WHEN constructing a DATATYPE_BREAKDOWN Claim for a gene-disease pair, THE Claim_Builder SHALL set its value to the list of Datatype_Scores for that pair from the Open Targets association Evidence_Record and SHALL reference that Evidence_ID.
5. WHEN constructing a LITERATURE_COUNT Claim for the gene, THE Claim_Builder SHALL set its value to the Literature_Count parsed from the gene-only PubMed Evidence_Record and SHALL reference that Evidence_ID.
6. WHEN constructing a CO_MENTION_COUNT Claim for a gene-disease pair, THE Claim_Builder SHALL set its value to the Co_Mention_Count parsed from the gene-plus-disease PubMed Evidence_Record and SHALL reference that Evidence_ID.
7. WHEN constructing a RECENT_PAPER Claim, THE Claim_Builder SHALL set its value to a single PMID drawn from the idlist of a PubMed Evidence_Record and SHALL reference that Evidence_ID.
8. WHERE any component proposes Claims from model output, THE Claim_Ledger SHALL accept those proposals only as structured data and SHALL NOT accept model-authored final prose.

### Requirement 6: Claim Verification

**User Story:** As a researcher, I want a verifier that rejects any claim whose evidence is missing or whose number does not match its evidence, so that I can trust every verified statement.

#### Acceptance Criteria

1. WHEN the Verifier evaluates a Claim, THE Verifier SHALL recompute the Claim value from the referenced Evidence_Record and SHALL compare the recomputed value with the Claim value.
2. IF a Claim references an Evidence_ID that is not present in the Evidence_Store, THEN THE Verifier SHALL reject the Claim with a reason that identifies the missing Evidence_ID.
3. IF the Claim value differs from the value recomputed from the referenced Evidence_Record, THEN THE Verifier SHALL reject the Claim with a reason that reports the Claim value and the recomputed value.
4. WHEN a Claim references an Evidence_ID present in the Evidence_Store and its value equals the value recomputed from that Evidence_Record, THE Verifier SHALL accept the Claim as a Verified_Claim.
5. WHEN comparing numeric Overall_Score or Datatype_Score values, THE Verifier SHALL treat the Claim value and the recomputed value as equal only when their absolute difference is within a fixed tolerance of 1e-9.
6. WHEN comparing Literature_Count, Co_Mention_Count, or PMID values, THE Verifier SHALL require exact equality.
7. THE Verifier SHALL NOT modify, repair, or substitute any Claim value during verification.

### Requirement 7: Unsupported-Claim Display

**User Story:** As a researcher, I want rejected claims shown as Unsupported with the reason, so that I can see what was excluded and why instead of silently losing it.

#### Acceptance Criteria

1. WHEN the Verifier rejects a Claim, THE Claim_Ledger SHALL display the Claim in the ledger as Unsupported together with the rejection reason.
2. THE Claim_Ledger SHALL NOT include any Unsupported_Claim in the rendered dossier prose.
3. THE Claim_Ledger SHALL NOT silently discard a rejected Claim without recording it as an Unsupported_Claim with a reason.
4. WHEN displaying an Unsupported_Claim, THE Claim_Ledger SHALL show the Claim type and the referenced Evidence_IDs alongside the reason.

### Requirement 8: Deterministic Dossier Rendering

**User Story:** As a researcher, I want the dossier text rendered deterministically from verified claims only, so that the same verified claims always produce the same report without a language model.

#### Acceptance Criteria

1. THE Dossier_Renderer SHALL render dossier prose using only Verified_Claims.
2. WHEN the Dossier_Renderer is given the same set of Verified_Claims, THE Dossier_Renderer SHALL produce identical dossier text on every invocation.
3. THE Dossier_Renderer SHALL order dossier content by a fixed, deterministic ordering of Verified_Claims that does not depend on retrieval order or dictionary iteration order.
4. THE Dossier_Renderer SHALL render dossier prose without invoking any language model at runtime.
5. WHERE a Verified_Claim references an Overall_Score, THE Dossier_Renderer SHALL present that value as an evidence-strength score and SHALL NOT present it as a probability.

### Requirement 9: Named Symmetric Conflict Rules

**User Story:** As a researcher, I want two named conflict rules shown side by side with thresholds I can see and configure, so that I can judge the quality of each association.

#### Acceptance Criteria

1. THE Conflict_Detector SHALL evaluate the Single_Source_Dependence rule and the Literature_Gap rule for each top association.
2. WHEN fewer than the configurable Minimum_Supporting_Datatype_Count datatypes of an association have a Datatype_Score at or above the configurable Support_Threshold, THE Conflict_Detector SHALL flag that association with the named Single_Source_Dependence conflict.
3. WHEN an association's Overall_Score meets or exceeds the configurable High_Score_Threshold, defaulting to 0.5, while its Co_Mention_Count is at or below the configurable Low_Co_Mention_Threshold, defaulting to 100, THE Conflict_Detector SHALL flag that association with the named Literature_Gap conflict.
4. WHEN the Claim_Ledger displays a flagged association, THE Claim_Ledger SHALL show the conflicting evidence for both sides side by side and SHALL NOT hide either side.
5. WHEN the Claim_Ledger displays an association evaluated by the Single_Source_Dependence rule, THE Claim_Ledger SHALL show the datatype scores that were counted, naming each datatype that met the Support_Threshold together with its Datatype_Score.
6. THE Claim_Ledger SHALL display in the user interface the current value of the Minimum_Supporting_Datatype_Count, defaulting to 2, the Support_Threshold, defaulting to 0.3, the High_Score_Threshold, defaulting to 0.5, and the Low_Co_Mention_Threshold, defaulting to 100.
7. WHEN a user changes the Minimum_Supporting_Datatype_Count, the Support_Threshold, the High_Score_Threshold, or the Low_Co_Mention_Threshold in the user interface, THE Conflict_Detector SHALL re-evaluate the conflict rules using the updated values.
8. THE Conflict_Detector SHALL name each flagged conflict in the user interface using the terms Single_Source_Dependence and Literature_Gap.

### Requirement 10: Wording and Safety Constraints

**User Story:** As a researcher, I want careful, consistent wording and a non-advice disclaimer, so that the application presents evidence without overstating it or implying medical guidance.

#### Acceptance Criteria

1. WHEN describing a gene-disease relationship, THE Claim_Ledger SHALL use the phrases "associated with" or "reported in".
2. THE Claim_Ledger SHALL NOT state that a gene causes a disease and SHALL NOT use the terms "causes", "proves", "validated", or "promising" to describe a gene-disease relationship.
3. THE Claim_Ledger SHALL NOT recommend a drug, a target, or a treatment, and SHALL NOT present medical advice.
4. THE Claim_Ledger SHALL display the notice "Evidence summary, not medical advice" on every results screen.
5. WHEN presenting an Open Targets Overall_Score, THE Claim_Ledger SHALL label the value as an evidence-strength score and SHALL NOT label it as a probability.
6. THE Claim_Ledger SHALL display a note clarifying that verification confirms a displayed number matches its saved source evidence and SHALL NOT state that verification confirms the finding or claim is true.
7. THE Claim_Ledger SHALL apply the rules in .kiro/steering/evidence-rules.md as non-negotiable constraints wherever those rules apply.

### Requirement 11: Offline and Cached Resilience

**User Story:** As a researcher, I want the application to work from cached responses when an API is unavailable, so that a demonstration with example genes succeeds offline.

#### Acceptance Criteria

1. THE Claim_Ledger SHALL include at least three preloaded Example_Genes, which include EGFR, TP53, and BRCA1.
2. THE Claim_Ledger SHALL include in the repository under data/cache the generated cached responses for each of EGFR, TP53, and BRCA1, and for the additional lesser-studied Example_Gene, so that those Example_Genes work offline.
3. THE Claim_Ledger SHALL include a fourth Example_Gene whose symbol is to be selected during implementation such that its cached data produces at least one conflict flag at the default thresholds (Support_Threshold 0.3, Minimum_Supporting_Datatype_Count 2, High_Score_Threshold 0.5, Low_Co_Mention_Threshold 100).
4. WHEN a user selects an Example_Gene while external access is unavailable, THE Claim_Ledger SHALL complete resolution, retrieval, claim construction, verification, and dossier rendering using only cached responses.
5. IF a request to an external interface fails or times out while a cached response for that request exists in the Response_Cache, THEN THE Claim_Ledger SHALL use the cached response and SHALL indicate that the result came from cache.
6. IF a request to an external interface fails or times out and no cached response for that request exists, THEN THE Claim_Ledger SHALL display a message that the interface is unavailable and SHALL continue with the evidence already retrieved.
7. WHEN a cached response is used, THE Evidence_Store SHALL create the corresponding Evidence_Record from the cached response with its original source, url, and retrieved_at.

### Requirement 12: Testing Strategy

**User Story:** As a maintainer, I want property-based and example tests for the critical logic, so that the verifier, rendering, conflict rules, and score recomputation are protected against regressions.

#### Acceptance Criteria

1. THE Test_Suite SHALL use pytest as the test runner and Hypothesis for property-based tests.
2. THE Test_Suite SHALL include a property-based test asserting that the Verifier accepts a Claim when and only when the referenced Evidence_ID exists in the Evidence_Store and the Claim value equals the value recomputed from that Evidence_Record.
3. THE Test_Suite SHALL include a property-based test asserting that the Dossier_Renderer produces identical output for the same set of Verified_Claims regardless of input ordering.
4. THE Test_Suite SHALL include a property-based test asserting that each conflict rule yields the same verdict for a given association and threshold set irrespective of the ordering of datatype scores and the ordering in which the two sides are supplied.
5. THE Test_Suite SHALL include a property-based test asserting that score recomputation reproduces the Overall_Score and Datatype_Scores from a given Open Targets association Evidence_Record within the Verifier numeric tolerance.
6. THE Test_Suite SHALL include a property-based round-trip test asserting that parsing an evidence payload, serializing the resulting Claims, and parsing them again yields equivalent Claims.
7. THE Test_Suite SHALL include a property-based test asserting that tampering with any single Claim value causes the Verifier to reject that Claim as an Unsupported_Claim and that resetting the tampered Claim to its original value causes the Verifier to accept it again as a Verified_Claim.
8. WHERE a test exercises external-interface behavior rather than application logic, THE Test_Suite SHALL use a small fixed set of representative cached examples rather than property-based generation.

### Requirement 13: Tamper Demonstration

**User Story:** As a reviewer, I want to deliberately tamper with one displayed claim number and watch verification reject it, so that I can see the verifier actually works.

#### Acceptance Criteria

1. THE Claim_Ledger SHALL provide a Tamper_Control that deliberately alters the displayed value of one selected Claim and re-runs the Verifier on that Claim.
2. WHEN the Tamper_Control alters a Claim value and the Verifier re-runs, THE Claim_Ledger SHALL display that Claim as Unsupported with a reason reporting both the tampered Claim value and the value recomputed from its Evidence_Record.
3. THE Claim_Ledger SHALL provide a reset control that restores the tampered Claim to its original value and re-runs the Verifier so that the Claim returns to Verified.
4. WHEN the Tamper_Control alters a Claim value, THE Claim_Ledger SHALL leave the Evidence_Record and the cached response unchanged and SHALL alter only the Claim value.

### Requirement 14: Usability and Accessibility

**User Story:** As a first-time user, I want clear explanations, legible status, and exportable output, so that I can understand and reuse the dossier.

#### Acceptance Criteria

1. THE Claim_Ledger SHALL display a "Why this matters" panel that explains who benefits, gives a plain-language explanation of the claim ledger, and lists the data sources Open Targets and PubMed.
2. THE Claim_Ledger SHALL display a "How to read this" legend that explains the claim statuses Verified and Unsupported and the conflict flags Single_Source_Dependence and Literature_Gap.
3. THE Claim_Ledger SHALL indicate each claim status using text and SHALL NOT convey status by color alone.
4. THE Claim_Ledger SHALL present status and text with sufficient contrast for readability.
5. THE Claim_Ledger SHALL provide a download control that exports the Verified_Claims and the Unsupported_Claims as JSON and the dossier as Markdown.

## Correctness Properties

- Verifier soundness and completeness: For any Claim and Evidence_Store, the Verifier accepts the Claim if and only if the referenced Evidence_ID is present and the Claim value equals the recomputed value within the defined tolerance (exact for integer counts and PMIDs). (Requirement 6)
- No claim without evidence: Every Claim carries at least one Evidence_ID, and no Verified_Claim references a missing Evidence_ID. (Requirements 5, 6)
- Rendering determinism: For any fixed set of Verified_Claims, repeated rendering and permuted input orderings produce byte-identical dossier text. (Requirement 8)
- Rendering provenance: No Unsupported_Claim contributes to rendered dossier prose. (Requirements 7, 8)
- Conflict symmetry: Each conflict rule yields the same verdict for the same inputs and thresholds regardless of the ordering of datatype scores and the ordering in which the two sides are supplied; in particular the Single_Source_Dependence verdict depends only on how many datatypes reach the Support_Threshold relative to the Minimum_Supporting_Datatype_Count, not on datatype ordering. (Requirement 9)
- Score recomputation fidelity: Recomputing the Overall_Score and Datatype_Scores from an association Evidence_Record reproduces the stored values within tolerance. (Requirements 2, 6)
- Parse round-trip: Parsing an evidence payload into Claims, serializing, and parsing again yields equivalent Claims. (Requirement 12)
- No-repair invariant: Verification never mutates a Claim value; a rejected Claim's value is unchanged after verification. (Requirements 6, 13)
- Tamper rejection and reset: Tampering with any single Claim value causes the Verifier to reject it as Unsupported, and resetting it to its original value causes the Verifier to accept it again as Verified, without any change to the Evidence_Record or cached response. (Requirements 12, 13)
