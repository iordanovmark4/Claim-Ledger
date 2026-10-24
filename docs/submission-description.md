# Claim Ledger: submission text

**Track:** Healthcare & Biotech (H4, accelerating biotechnology innovation)

**One line:** Every claim tied to its evidence. A gene-disease dossier where nothing appears unless it is checked against a saved source.

## The problem
Researchers, students and small labs constantly rely on summaries of what is "known" about a gene and its diseases. Many tools, including AI assistants, write fluent text first and cite later, so a reader cannot tell what was checked. Paid literature-intelligence tools exist, but many students and small labs cannot afford them.

## What it does
Enter a human gene symbol (or pick an example). Claim Ledger pulls disease associations from Open Targets and literature counts from PubMed, saves every raw response with its source, URL and retrieval time, and turns each result into a structured claim. A verifier accepts a claim only if its evidence exists and every number matches the value recomputed from that evidence. Rejected claims are not hidden: they appear as Unsupported with the reason.

- **Ledger:** every claim with type, value, status and Evidence ID.
- **Dossier:** plain-language text rendered only from verified claims, deterministically, with no language model at runtime.
- **Two conflict rules**, with visible thresholds you can change live. Single_Source_Dependence flags an association supported by too few independent kinds of evidence. Literature_Gap flags a high-scoring association that few papers actually discuss. Both sides are shown side by side.
- **Tamper demonstration:** alter one number on purpose and watch the verifier reject it.
- **Works offline** for four example genes (EGFR, TP53, BRCA1, PANK2) from bundled cached responses. PANK2 shows both kinds of conflict flag on real data; EGFR shows none.
- **Export** the ledger as JSON and the dossier as Markdown.

## Who benefits and how it scales
Students, early-career researchers and small labs who need to sanity-check claimed gene-disease links without paid tools. Because the ledger and verifier are independent of the interface, the same checking layer could sit behind any AI research assistant that proposes claims as structured data, so its answers can be audited the same way.

## Accessibility and honesty
Status is shown in words, not just color. A "How to read this" legend explains each label. The app states clearly that an Open Targets score is an evidence-strength score, not a probability, and that verification confirms a number matches its saved source, not that a finding is true. It never recommends a treatment or gives medical advice.

## Data
Open Targets Platform (GraphQL API) and PubMed (NCBI E-utilities), both listed among the challenge's biotechnology resources.

## How we used Kiro
Requirements-first specs (14 requirements, a ~700-line design, 19 tasks), a project steering file that forced parsers to be built against saved real responses, two agent hooks, a small MCP server that lets Kiro call the verifier directly, and Hypothesis property tests for 11 correctness properties, one of which caught a real Unicode bug. See docs/kiro-writeup.md.

## Limits
Association scores and PubMed counts depend on those sources and on how queries are phrased. PubMed counts come from title and abstract matching, which is a rough proxy for how much a pair is discussed. The tool summarizes evidence; it does not establish causation.

## Try it
Live demo: https://claim-ledger-hdzvxbdl2jalvcmnyiszhn.streamlit.app/ (works offline for EGFR, TP53, BRCA1 and PANK2, no login or keys needed). Run locally: see README.md.
