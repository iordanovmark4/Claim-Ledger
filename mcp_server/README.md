# Claim Ledger MCP server

A small, **read-only** [Model Context Protocol](https://modelcontextprotocol.io)
server that exposes the Claim Ledger evidence pipeline as two tools. It reuses
the existing `claimledger` package unchanged and serves everything from the
committed cache under `data/cache`, so it works fully offline.

It speaks MCP over stdio (JSON-RPC 2.0, one JSON message per line) using only the
Python standard library plus `claimledger` — there is no third-party MCP SDK
dependency.

## Tools

### `lookup_gene_evidence(symbol)`

Runs the offline-capable pipeline for a gene symbol and returns its structured
evidence as JSON:

- `verified_claims` — the accepted claims, each with its `evidence_ids`.
- `unsupported_claims` — the rejected claims, each with a `reason` and its
  `evidence_ids` (never silently dropped).
- `conflict_flags` — any `Single_Source_Dependence` / `Literature_Gap` flags,
  with both sides of the evidence shown (counted datatypes; overall score beside
  the co-mention count) and the thresholds in force.
- `ensembl_id`, `resolve_status`, `literature_count`, `co_mention_counts`,
  and `all_from_cache` / `any_from_cache`.

Example symbols that work offline: `EGFR`, `TP53`, `BRCA1`, `PANK2` (PANK2 trips
conflict flags at the default thresholds).

### `verify_claim(claim_json, gene_symbol)`

Re-checks one claim against its saved evidence and returns `Verified` or
`Unsupported`. Pass a claim object in the exact shape returned by
`lookup_gene_evidence` (a dict, or a JSON string of one) plus the gene it belongs
to. The server rebuilds the per-run evidence store offline and runs the real
`Verifier`, which recomputes the value from the cached evidence and compares:

- a match returns `{"status": "Verified"}`;
- a mismatch (e.g. a tampered number) returns `{"status": "Unsupported", ...}`
  with a reason reporting **both** the claim value and the recomputed value.

The Verifier is reused unchanged — it never mutates or repairs a claim value.

## Evidence rules honored

This server is subordinate to `.kiro/steering/evidence-rules.md`:

- Every returned claim carries at least one `Evidence_ID` that exists in the
  current run's store (rules 1, 2).
- Numbers are never authored here; they are recomputed from the cached real
  responses by the pipeline (rules 3, 4, 8).
- Mismatches are reported, never repaired; unsupported claims are returned with a
  reason, never silently kept (rules 3, 6).
- Conflicting evidence is shown side by side (rule 7).
- The server proposes claims only as structured JSON, writes no prose, and
  recommends nothing (rules 4, 5, 10).

Both tools are read-only: they never write to `data/cache` and never mutate an
evidence record.

## Install

```sh
pip install -r mcp_server/requirements.txt
```

`claimledger` is imported from `../src` via `sys.path`, so no package install is
required. (Alternatively, `pip install -e .` from the repo root installs it.)

## Run

Launched automatically by Kiro from the workspace MCP config
(`.kiro/settings/mcp.json`). To run it by hand from the repo root:

```sh
python -m mcp_server.server
```

It reads newline-delimited JSON-RPC requests on stdin and writes responses to
stdout. Supported methods: `initialize`, `tools/list`, `tools/call`.

## Registration

Registered in the workspace config at `.kiro/settings/mcp.json`:

```json
{
  "mcpServers": {
    "claim-ledger": {
      "command": "python",
      "args": ["-m", "mcp_server.server"],
      "env": {},
      "disabled": false,
      "autoApprove": ["lookup_gene_evidence", "verify_claim"]
    }
  }
}
```
