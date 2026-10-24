# Claim Ledger

**Every claim tied to its evidence.**

Claim Ledger is a Streamlit web app that turns a human gene symbol into an
auditable, evidence-backed dossier of its reported disease associations. Every
number on screen is a structured *claim* linked to at least one piece of saved
evidence, and a verifier recomputes each number from its source: a claim is
shown as **Verified** only when the number matches, and as **Unsupported** (with
a reason) otherwise. Nothing is silently dropped. The dossier prose is rendered
deterministically from verified claims only — no language model runs at runtime.

This is an evidence-summary tool. It never recommends a drug, target, or
treatment, and it is not medical advice. An Open Targets score is shown as an
evidence-strength score, not a probability.

## Data sources

- **Open Targets** (GraphQL API) — gene-to-target resolution, gene-disease
  association scores, and the per-datatype score breakdown.
- **PubMed** (NCBI E-utilities `esearch`) — literature counts for the gene, the
  gene-plus-disease co-mention counts, and recent paper PMIDs. Requests are
  rate-limited and carry a `tool` and `email` identifier.

Every API response is stored as an evidence record (source, URL, retrieved-at)
and cached under `data/cache`, so parsing is always written against real saved
responses.

## Conflicts

Two named conflict rules are surfaced side by side with configurable thresholds:

- **Single_Source_Dependence** — the association is supported by too few
  datatypes (fewer than the minimum reach the support threshold).
- **Literature_Gap** — a high association score paired with a very low
  gene-plus-disease PubMed co-mention count.

## Run the app

Requires Python 3.10+.

```bash
pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

The app opens in your browser. The four example genes work fully offline from
the committed cache (see below); other genes require network access to Open
Targets and PubMed. A sidebar toggle ("Offline (cached only)") keeps the app on
cached data only.

Deploying to Streamlit Community Cloud: point it at `streamlit_app.py` as the
main file — it puts `src/` on the path, so no install step beyond
`requirements.txt` is needed.

## Offline example genes

Four example genes ship with their real Open Targets and PubMed responses
committed under `data/cache`, so they resolve, verify, and render with no
network:

- **EGFR** — a heavily studied oncogene (no conflict flags at default thresholds).
- **TP53** — a well-studied tumor suppressor.
- **BRCA1** — a well-studied DNA-repair gene.
- **PANK2** — a lesser-studied Mendelian gene, included because its real data
  trips conflict flags at the default thresholds (both Literature_Gap, from a
  high association score with very few co-mentions, and Single_Source_Dependence).

## Run the tests

```bash
python -m pytest -q
```

The suite uses `pytest` with `hypothesis` property-based tests covering the
verifier (soundness, completeness, no-repair), deterministic rendering,
conflict-rule symmetry, score recomputation, parse round-trips, the tamper/reset
behavior, and Evidence_ID stability, plus example-based tests that parse the
committed real API responses and an end-to-end offline run of all four example
genes.

## Project layout

- `src/claimledger/` — the package: config, models, HTTP cache/fetcher, evidence
  store, Open Targets and PubMed clients, the shared recompute functions, claim
  builder, verifier, conflict detector, dossier renderer, exporter, pipeline, and
  the Streamlit `app`.
- `tests/` — pytest + Hypothesis tests, with committed real response fixtures.
- `data/cache/` — committed raw API responses (the offline cache).
- `streamlit_app.py` — the entry point.
- `.streamlit/config.toml` — the app theme.