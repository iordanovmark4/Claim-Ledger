"""Claim Ledger MCP server: two read-only, evidence-first tools over the pipeline.

This is a self-contained Model Context Protocol server speaking JSON-RPC 2.0 over
stdio (newline-delimited messages on stdin/stdout). It exposes exactly two
read-only tools that *reuse* the existing ``claimledger`` package rather than
re-deriving any logic:

1. ``lookup_gene_evidence(symbol)`` runs the offline-capable pipeline
   (``claimledger.pipeline.Pipeline`` over an offline ``HttpFetcher`` served from
   the committed ``data/cache``) for a gene and returns the Verified claims, the
   Unsupported claims (each with its reason), and any conflict flags as JSON,
   every claim carrying its Evidence_IDs.

2. ``verify_claim(claim_json, gene_symbol)`` rebuilds the same per-run evidence
   store offline for ``gene_symbol`` and re-checks the supplied claim against its
   saved evidence through the real ``claimledger.verify.Verifier``, returning
   ``Verified`` or ``Unsupported`` with the reason (reporting both the claim value
   and the value recomputed from the evidence on a mismatch).

Design constraints honored (the project's non-negotiable evidence rules):

* Both tools are strictly read-only. They never write to ``data/cache``, never
  mutate an EvidenceRecord, and never "repair" a claim value — ``verify_claim``
  reuses the Verifier unchanged, which recomputes-and-compares and rejects
  mismatches (rules 3, 5, 6, 8).
* Every claim returned carries at least one Evidence_ID (rule 1), and each
  Evidence_ID is one that genuinely exists in the current run's store (rule 2).
* Numbers are never authored here. They come from the pipeline, which recomputes
  them from the cached real responses (rules 3, 4, 8).
* The server proposes claims only as structured JSON; it writes no prose and
  recommends nothing (rules 4, 5, 10).

The server has no third-party runtime dependency: it imports only the standard
library and the local ``claimledger`` package. It implements the small slice of
the MCP protocol a client needs (``initialize``, ``tools/list``, ``tools/call``).

Run directly as ``python -m mcp_server.server`` (from the repo root) or
``python server.py`` (from ``mcp_server/``); the module inserts the repo ``src``
directory on ``sys.path`` so ``import claimledger`` works without installation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

# --- make `import claimledger` work without installation --------------------- #
# mcp_server/server.py -> mcp_server -> repo root; the package lives in src/.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from claimledger.cache import ResponseCache  # noqa: E402
from claimledger.claims import ClaimBuilder  # noqa: E402
from claimledger.clients.opentargets import OTClient, ResolveStatus  # noqa: E402
from claimledger.clients.pubmed import (  # noqa: E402
    PubMedClient,
    build_esearch_params,
    build_esearch_url,
    gene_term,
    pair_term,
)
from claimledger.config import Config  # noqa: E402
from claimledger.evidence import (  # noqa: E402
    EvidenceStore,
    mint_evidence_id,
    ot_associations_request_key,
    pubmed_request_key,
)
from claimledger.http import HttpFetcher, RateLimiter  # noqa: E402
from claimledger.models import (  # noqa: E402
    Association,
    Claim,
    ClaimStatus,
    ConflictFlag,
    DatatypeScore,
    Source,
)
from claimledger.pipeline import Pipeline  # noqa: E402
from claimledger.verify import Verifier  # noqa: E402

SERVER_NAME = "claim-ledger"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"


# --------------------------------------------------------------------------- #
# Offline wiring (reused by both tools) — never touches the network and never #
# writes to the cache.                                                        #
# --------------------------------------------------------------------------- #
def _offline_fetcher(config: Config) -> HttpFetcher:
    """Build an ``HttpFetcher`` that only ever serves the committed cache.

    ``external_available`` is pinned to ``False`` so a present cache file is
    returned cache-first and the live request is never attempted (offline). The
    rate limiter's clock/sleep are faked so no real time passes. Because nothing
    is ever fetched live, nothing is ever written to ``data/cache`` — the tools
    are strictly read-only against the evidence store.
    """
    return HttpFetcher(
        cache=ResponseCache(),  # defaults to the repo data/cache (committed)
        rate_limiter=RateLimiter(clock=lambda: 0.0, sleep=lambda _s: None),
        config=config,
        external_available=lambda: False,
    )


def _datatype_scores_to_json(
    scores: tuple[DatatypeScore, ...] | Any,
) -> list[dict[str, Any]]:
    """Serialize a DATATYPE_BREAKDOWN value (tuple of DatatypeScore) to JSON."""
    out: list[dict[str, Any]] = []
    for ds in scores:
        out.append({"id": ds.id, "score": ds.score})
    return out


def _claim_value_to_json(claim: Claim) -> Any:
    """Return a JSON-ready view of a claim value, mirroring ``Claim.to_dict``.

    Only ``DATATYPE_BREAKDOWN`` needs structural conversion (tuple of
    DatatypeScore -> list of dicts); every other claim type's value is already a
    JSON scalar (float / int / str PMID).
    """
    # Claim.to_dict already encodes the per-type rule; reuse it so this server
    # never invents a second, divergent serialization.
    return claim.to_dict()["value"]


def _claim_to_json(claim: Claim) -> dict[str, Any]:
    """Serialize a Claim to the tool's JSON shape, carrying its Evidence_IDs.

    Reuses ``Claim.to_dict`` (the model's own explicit serializer) so the shape
    round-trips exactly through ``Claim.from_dict`` — which is what
    ``verify_claim`` consumes. Every claim carries ``evidence_ids`` (rule 1).
    """
    return claim.to_dict()


def _conflict_flag_to_json(flag: ConflictFlag) -> dict[str, Any]:
    """Serialize a ConflictFlag, showing both sides of the evidence (rule 7).

    Both named rules are rendered with their detail so neither side is hidden:
    ``Single_Source_Dependence`` exposes the counted supporting datatypes;
    ``Literature_Gap`` exposes the overall score beside the co-mention count. The
    thresholds in force when flagged are included for provenance.
    """
    thresholds = flag.thresholds
    thresholds_json: Optional[dict[str, Any]] = None
    if thresholds is not None:
        thresholds_json = {
            "support_threshold": thresholds.support_threshold,
            "min_supporting_datatypes": thresholds.min_supporting_datatypes,
            "high_score_threshold": thresholds.high_score_threshold,
            "low_co_mention_threshold": thresholds.low_co_mention_threshold,
        }
    return {
        "rule": flag.rule,
        "disease_id": flag.disease_id,
        "disease_name": flag.disease_name,
        "supporting_datatypes": _datatype_scores_to_json(flag.supporting_datatypes),
        "supporting_count": flag.supporting_count,
        "overall_score": flag.overall_score,
        "co_mention_count": flag.co_mention_count,
        "thresholds": thresholds_json,
    }


# --------------------------------------------------------------------------- #
# Tool 1: lookup_gene_evidence                                                #
# --------------------------------------------------------------------------- #
def lookup_gene_evidence(symbol: str) -> dict[str, Any]:
    """Run the offline pipeline for ``symbol`` and return its structured evidence.

    Returns a JSON-serializable dict with:

    * ``gene_symbol`` / ``ensembl_id`` / ``resolve_status`` — what resolved;
    * ``verified_claims`` — the accepted claims (each with Evidence_IDs);
    * ``unsupported_claims`` — the rejected claims, each with its reason and
      Evidence_IDs (never silently dropped, rule 6);
    * ``conflict_flags`` — any Single_Source_Dependence / Literature_Gap flags,
      both sides shown (rule 7);
    * ``from_cache`` — whether the evidence came from the committed cache.

    When the symbol does not resolve offline, the structured result still comes
    back with the resolve status and empty claim lists (rather than an error), so
    a caller can branch on ``resolve_status``.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a non-empty string")

    config = Config()
    pipeline = Pipeline(config=config, fetcher=_offline_fetcher(config))
    result = pipeline.run(symbol)

    return {
        "gene_symbol": result.gene_symbol,
        "ensembl_id": result.ensembl_id,
        "resolve_status": result.status.value
        if hasattr(result.status, "value")
        else str(result.status),
        "verified_claims": [_claim_to_json(c) for c in result.verified_claims],
        "unsupported_claims": [_claim_to_json(c) for c in result.unsupported_claims],
        "conflict_flags": [
            _conflict_flag_to_json(f) for f in result.conflict_flags
        ],
        "literature_count": result.literature_count,
        "co_mention_counts": result.co_mention_counts,
        "any_from_cache": result.any_from_cache,
        "all_from_cache": result.all_from_cache,
        "notice": "Evidence summary, not medical advice.",
    }


# --------------------------------------------------------------------------- #
# Tool 2: verify_claim                                                        #
# --------------------------------------------------------------------------- #
def _build_run_store(gene_symbol: str, config: Config) -> EvidenceStore:
    """Rebuild the per-run EvidenceStore for ``gene_symbol`` from the cache.

    This mirrors the pipeline's own evidence gathering exactly — same offline
    fetcher, same clients, same request keys — so that the Evidence_IDs present
    in the returned store are identical to the ones a claim from
    ``lookup_gene_evidence`` references. Re-verifying a claim then checks it
    against the very evidence it was built from (rules 2, 3).

    Raises ``ValueError`` if the gene does not resolve offline (no store to build).
    """
    store = EvidenceStore()
    fetcher = _offline_fetcher(config)
    ot = OTClient(fetcher, store, config)
    pubmed = PubMedClient(fetcher, store, config)

    outcome = ot.resolve(gene_symbol)
    if outcome.status is not ResolveStatus.RESOLVED:
        raise ValueError(
            f"gene {gene_symbol!r} did not resolve offline "
            f"(status={getattr(outcome.status, 'value', outcome.status)}); "
            f"no evidence store to verify against"
        )
    ensembl_id = outcome.ensembl_id
    symbol = outcome.resolved_symbol or gene_symbol
    top_n = config.thresholds.top_n

    # OT associations (and its Evidence_ID), then gene-only + per-pair PubMed —
    # exactly the pipeline's sequence so every Evidence_ID lines up.
    ot.associated_diseases(ensembl_id, top_n=top_n)
    pubmed.esearch_gene(symbol)
    target_assoc = ot.associated_diseases(ensembl_id, top_n=top_n)
    for assoc in target_assoc.associations:
        pubmed.esearch_pair(symbol, assoc.disease_name)

    return store


def verify_claim(claim_json: Any, gene_symbol: str) -> dict[str, Any]:
    """Re-check ``claim_json`` against its saved evidence for ``gene_symbol``.

    ``claim_json`` is a claim object in the shape produced by
    ``lookup_gene_evidence`` (i.e. ``Claim.to_dict``); it may be passed as a dict
    or as a JSON string. The claim is parsed with ``Claim.from_dict``, the per-run
    evidence store for ``gene_symbol`` is rebuilt offline, and the real
    ``Verifier`` recomputes-and-compares.

    Returns a dict with:

    * ``status`` — ``"Verified"`` or ``"Unsupported"``;
    * ``reason`` — ``None`` when Verified; on a mismatch, a message reporting both
      the claim value and the value recomputed from the evidence;
    * ``claim_value`` / ``recomputed_value`` — both sides, for display;
    * ``evidence_ids`` — the Evidence_IDs the claim references.

    The Verifier is reused unchanged: it never mutates or repairs the claim value
    (rule 3). A claim whose Evidence_ID is absent from the store, or whose number
    does not match the recomputed value, comes back ``Unsupported`` with the
    reason — it is never silently accepted or repaired (rules 3, 6).
    """
    if isinstance(claim_json, str):
        claim_json = json.loads(claim_json)
    if not isinstance(claim_json, dict):
        raise ValueError("claim_json must be a claim object (dict) or a JSON string")
    if not isinstance(gene_symbol, str) or not gene_symbol.strip():
        raise ValueError("gene_symbol must be a non-empty string")

    claim = Claim.from_dict(claim_json)

    config = Config()
    store = _build_run_store(gene_symbol, config)

    result = Verifier().verify(claim, store)

    # Render both values in a JSON-safe way (a DATATYPE_BREAKDOWN recomputes to a
    # tuple of DatatypeScore; everything else is already a scalar).
    def _value_json(value: Any) -> Any:
        if isinstance(value, tuple):
            try:
                return _datatype_scores_to_json(value)
            except Exception:
                return [repr(v) for v in value]
        return value

    return {
        "status": result.status.value
        if hasattr(result.status, "value")
        else str(result.status),
        "verified": result.status is ClaimStatus.VERIFIED,
        "reason": result.reason,
        "claim_type": claim.claim_type.value,
        "subject": claim.subject,
        "evidence_ids": list(claim.evidence_ids),
        "claim_value": _value_json(result.claim_value),
        "recomputed_value": _value_json(result.recomputed_value),
        "notice": (
            "Verification confirms a displayed number matches its saved source "
            "evidence; it does not confirm the finding is true."
        ),
    }


# --------------------------------------------------------------------------- #
# Tool registry + JSON Schemas                                                #
# --------------------------------------------------------------------------- #
TOOLS: list[dict[str, Any]] = [
    {
        "name": "lookup_gene_evidence",
        "description": (
            "Run the offline-capable Claim Ledger pipeline for a human gene "
            "symbol and return its structured evidence: the Verified claims, the "
            "Unsupported claims (each with a reason), and any conflict flags "
            "(Single_Source_Dependence / Literature_Gap), every claim carrying "
            "its Evidence_IDs. Read-only; served from the committed cache. "
            "Evidence summary, not medical advice."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Human gene symbol, e.g. EGFR, TP53, BRCA1, PANK2.",
                }
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
    {
        "name": "verify_claim",
        "description": (
            "Re-check a single claim against its saved evidence for a gene and "
            "return Verified or Unsupported with the reason. The claim must be in "
            "the shape returned by lookup_gene_evidence. Reuses the Verifier "
            "unchanged: it recomputes the value from the cached evidence and "
            "rejects mismatches without repairing them. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "claim_json": {
                    "type": ["object", "string"],
                    "description": (
                        "A claim object (as produced by lookup_gene_evidence), "
                        "or a JSON string of one."
                    ),
                },
                "gene_symbol": {
                    "type": "string",
                    "description": "The gene symbol the claim belongs to.",
                },
            },
            "required": ["claim_json", "gene_symbol"],
            "additionalProperties": False,
        },
    },
]

_TOOL_IMPLS = {
    "lookup_gene_evidence": lambda args: lookup_gene_evidence(args["symbol"]),
    "verify_claim": lambda args: verify_claim(
        args["claim_json"], args["gene_symbol"]
    ),
}


# --------------------------------------------------------------------------- #
# Minimal MCP (JSON-RPC 2.0 over stdio) transport                             #
# --------------------------------------------------------------------------- #
def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def handle_request(message: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Handle one JSON-RPC request/notification; return a response or ``None``.

    Notifications (no ``id``) return ``None`` (no response sent). Supports the
    MCP handshake (``initialize``), tool discovery (``tools/list``), and tool
    invocation (``tools/call``). A ``tools/call`` result follows the MCP content
    shape: a single ``text`` content block holding the tool's JSON payload, plus
    an ``isError`` flag when the tool raised.
    """
    method = message.get("method")
    request_id = message.get("id")
    is_notification = "id" not in message

    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method in ("notifications/initialized", "initialized"):
        return None  # handshake ack, nothing to return

    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        impl = _TOOL_IMPLS.get(name)
        if impl is None:
            if is_notification:
                return None
            return _error(request_id, -32602, f"unknown tool: {name!r}")
        try:
            payload = impl(arguments)
            content = [
                {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
            ]
            return _result(request_id, {"content": content, "isError": False})
        except Exception as exc:  # surface tool errors as an MCP tool error
            content = [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}]
            return _result(request_id, {"content": content, "isError": True})

    if is_notification:
        return None
    return _error(request_id, -32601, f"method not found: {method!r}")


def main() -> None:
    """Serve MCP over stdio: one JSON message per line on stdin -> stdout.

    Reads newline-delimited JSON-RPC messages from stdin and writes each response
    as a single JSON line to stdout (flushed immediately). Exits cleanly on EOF.
    Malformed JSON yields a JSON-RPC parse error response.
    """
    stdin = sys.stdin
    stdout = sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
            stdout.flush()
            continue
        response = handle_request(message)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


if __name__ == "__main__":
    main()
