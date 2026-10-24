"""Streamlit UI for Claim Ledger (Req 1, 9, 10, 13, 14).

A sleek, calm, scientific single-page app over the :class:`~claimledger.pipeline.Pipeline`.
Importing this module is side-effect-free: all Streamlit calls live inside
functions, and the page runs only when :func:`main` is called (by the root
``streamlit_app.py`` entry point, or ``python -m streamlit run src/claimledger/app.py``
would also work via the guard at the bottom). The test suite imports this module
without a Streamlit runtime.

Design (matches .streamlit/config.toml: deep navy #0B1220 canvas, teal #2DD4BF
accent, off-white #E8EDF4 text):
* a hero header with the app name, the promise, a one-sentence subtitle, and a
  row of three metric cards (Verified / Unsupported / Conflicts);
* a gene input + Analyze button and four "Try an example" buttons
  (EGFR, TP53, BRCA1, PANK2);
* a sidebar with the four conflict thresholds (showing current values) and an
  offline toggle;
* tabs: Dossier, Ledger, Conflicts, Evidence, Why this matters.

Accessibility: status is ALWAYS shown as text + a symbol, never color alone;
keyboard focus outlines are preserved and strengthened; contrast targets WCAG AA.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

import streamlit as st

from .config import Config, Thresholds
from .conflicts import LITERATURE_GAP, SINGLE_SOURCE_DEPENDENCE
from .clients.opentargets import ResolveStatus
from .export import Exporter
from .models import Claim, ClaimStatus, ClaimType, EvidenceRecord
from .pipeline import Pipeline, PipelineResult
from .render import SAFETY_NOTICE
from .tamper import reset as tamper_reset
from .tamper import tamper as tamper_apply

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

APP_NAME = "Claim Ledger"
APP_PROMISE = "Every claim tied to its evidence"
APP_SUBTITLE = (
    "Enter a human gene symbol to build an evidence-backed dossier of its "
    "reported disease associations, drawn from Open Targets and PubMed."
)

# Mandatory notices shown on every results screen (Req 10.4, 10.6).
MEDICAL_ADVICE_NOTICE = SAFETY_NOTICE  # "Evidence summary, not medical advice"
VERIFICATION_NOTE = (
    "Verification confirms a number matches its saved source, "
    "not that the finding is true."
)

# The four Example_Genes the UI offers (Req 11.1-11.3). Sourced from Config so
# the registry stays the single source of truth; filter out any unset slot.
EXAMPLE_GENES: tuple[str, ...] = tuple(
    g for g in Config().example_genes if isinstance(g, str) and g
)

# Status glyphs — a symbol paired with the text label so status is never
# conveyed by color alone (Req 10.3, 14.3).
_STATUS_SYMBOL = {
    ClaimStatus.VERIFIED: "\u2713",      # check mark
    ClaimStatus.UNSUPPORTED: "\u2717",   # ballot x
}


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested without a Streamlit runtime)                      #
# --------------------------------------------------------------------------- #

def validate_gene_input(raw: str) -> tuple[bool, str, Optional[str]]:
    """Validate a raw gene-input string (Req 1.4).

    Returns ``(ok, cleaned, message)``. The input is only stripped of
    surrounding whitespace — never upper-cased — because the resolver matches
    case-insensitively. An empty or whitespace-only input is invalid and yields
    a user-facing message with ``ok=False``; no retrieval should be issued for
    it.
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        return False, "", "Enter a gene symbol to analyze (for example, EGFR)."
    return True, cleaned, None


def get_pipeline(thresholds: Thresholds, offline: bool) -> Pipeline:
    """Build a :class:`Pipeline` for the given thresholds and offline mode.

    The config carries the thresholds so the conflict detector and top-N use the
    UI's current values. When ``offline`` is True the pipeline is given a
    cache-first fetcher with external access disabled, so the four Example_Genes
    resolve and render entirely from the committed ``data/cache`` with no
    network; otherwise the pipeline's default fetcher allows live calls but still
    serves cache-first on failure.
    """
    config = Config(thresholds=thresholds)
    if offline:
        from .cache import ResponseCache
        from .http import HttpFetcher, RateLimiter

        fetcher = HttpFetcher(
            cache=ResponseCache(),
            rate_limiter=RateLimiter(
                max_requests_per_second=config.max_requests_per_second
            ),
            config=config,
            external_available=lambda: False,
        )
        return Pipeline(config, fetcher=fetcher)
    return Pipeline(config)


def claim_value_display(claim: Claim) -> str:
    """Render a claim's value as a compact, human-readable string for the ledger.

    Deterministic and type-aware: a score to four decimals, a count with
    thousands separators, a PMID or a datatype breakdown summarized by id. Never
    fabricates — it only formats the value already on the claim.
    """
    value = claim.value
    if claim.claim_type is ClaimType.ASSOCIATION:
        try:
            return f"{float(value):.4f}"
        except (TypeError, ValueError):
            return str(value)
    if claim.claim_type in (ClaimType.LITERATURE_COUNT, ClaimType.CO_MENTION_COUNT):
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return str(value)
    if claim.claim_type is ClaimType.DATATYPE_BREAKDOWN and isinstance(value, tuple):
        parts = ", ".join(
            f"{ds.id} {float(ds.score):.3f}"
            for ds in sorted(value, key=lambda d: d.id)
        )
        return parts or "(none)"
    return str(value)


# --------------------------------------------------------------------------- #
# Styling                                                                     #
# --------------------------------------------------------------------------- #

_CSS = """
<style>
/* --- Claim Ledger theme: navy canvas, teal accent, calm scientific feel --- */
:root {
  --cl-bg: #0B1220;
  --cl-card: #141C2B;
  --cl-border: #243049;
  --cl-text: #E8EDF4;
  --cl-muted: #9FB0C8;
  --cl-accent: #2DD4BF;
  --cl-verified-bg: #10332E;
  --cl-verified-fg: #7FEAD9;
  --cl-unsupported-bg: #3A1F27;
  --cl-unsupported-fg: #F6B6C2;
}

/* generous, uncluttered spacing */
.block-container { padding-top: 2.2rem; max-width: 1100px; }

/* Hero */
.cl-hero { margin-bottom: 0.4rem; }
.cl-hero-name { font-size: 2.1rem; font-weight: 700; letter-spacing: -0.01em; color: var(--cl-text); margin: 0; }
.cl-hero-promise { color: var(--cl-accent); font-weight: 600; font-size: 1.05rem; margin: 0.15rem 0 0 0; }
.cl-hero-sub { color: var(--cl-muted); font-size: 0.98rem; margin: 0.5rem 0 0 0; max-width: 60ch; line-height: 1.5; }

/* Cards */
.cl-card {
  background: var(--cl-card);
  border: 1px solid var(--cl-border);
  border-radius: 14px;
  padding: 1.1rem 1.25rem;
  margin-bottom: 0.9rem;
}
.cl-metric {
  background: var(--cl-card);
  border: 1px solid var(--cl-border);
  border-radius: 14px;
  padding: 0.9rem 1.1rem;
  text-align: left;
}
.cl-metric .cl-metric-label { color: var(--cl-muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.06em; margin: 0; }
.cl-metric .cl-metric-value { color: var(--cl-text); font-size: 1.9rem; font-weight: 700; margin: 0.1rem 0 0 0; }

/* Status badges: text + symbol, never color alone */
.cl-badge {
  display: inline-block;
  padding: 0.12rem 0.6rem;
  border-radius: 999px;
  font-size: 0.85rem;
  font-weight: 600;
  border: 1px solid transparent;
  white-space: nowrap;
}
.cl-badge-verified { background: var(--cl-verified-bg); color: var(--cl-verified-fg); border-color: #1C5248; }
.cl-badge-unsupported { background: var(--cl-unsupported-bg); color: var(--cl-unsupported-fg); border-color: #6B2F3C; }

/* Notices */
.cl-notice {
  background: var(--cl-card);
  border: 1px solid var(--cl-border);
  border-left: 3px solid var(--cl-accent);
  border-radius: 10px;
  padding: 0.6rem 0.9rem;
  color: var(--cl-text);
  font-size: 0.9rem;
  margin-bottom: 0.5rem;
}
.cl-notice-strong { font-weight: 700; }

/* Side-by-side conflict layout */
.cl-sides { display: flex; gap: 0.9rem; flex-wrap: wrap; }
.cl-side {
  flex: 1 1 300px;
  background: var(--cl-card);
  border: 1px solid var(--cl-border);
  border-radius: 12px;
  padding: 0.9rem 1rem;
}
.cl-side h4 { margin: 0 0 0.4rem 0; font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--cl-muted); }
.cl-rule-name { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--cl-accent); font-weight: 600; }

/* Monospace Evidence_IDs */
.cl-mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.85rem; color: var(--cl-muted); }

/* Ledger table: aligned, subtle row separation, monospace ids */
.cl-ledger { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
.cl-ledger th {
  text-align: left; color: var(--cl-muted); font-weight: 600;
  font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em;
  padding: 0.5rem 0.6rem; border-bottom: 1px solid var(--cl-border);
}
.cl-ledger td { padding: 0.5rem 0.6rem; border-bottom: 1px solid rgba(36,48,73,0.5); color: var(--cl-text); vertical-align: top; }
.cl-ledger td.cl-id { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.82rem; color: var(--cl-muted); }
.cl-ledger td.cl-num { text-align: right; font-variant-numeric: tabular-nums; }
.cl-ledger tr:hover td { background: rgba(45,212,191,0.04); }

/* Legend */
.cl-legend-item { margin-bottom: 0.35rem; color: var(--cl-text); font-size: 0.9rem; }

/* Keyboard focus: never remove; strengthen for visibility (accessibility) */
a:focus-visible, button:focus-visible, input:focus-visible,
[role="button"]:focus-visible, [tabindex]:focus-visible {
  outline: 3px solid var(--cl-accent) !important;
  outline-offset: 2px !important;
}
</style>
"""


def inject_css() -> None:
    """Inject the stylesheet once per session (idempotent within a rerun)."""
    st.markdown(_CSS, unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Small render helpers                                                        #
# --------------------------------------------------------------------------- #

def _status_badge_html(status: ClaimStatus) -> str:
    """Pill badge HTML with a symbol AND the text label (never color alone)."""
    symbol = _STATUS_SYMBOL.get(status, "")
    cls = "cl-badge-verified" if status is ClaimStatus.VERIFIED else "cl-badge-unsupported"
    return f'<span class="cl-badge {cls}">{symbol} {status.value}</span>'


def render_notices() -> None:
    """Render the two mandatory notices shown on every results screen (Req 10.4, 10.6)."""
    st.markdown(
        f'<div class="cl-notice"><span class="cl-notice-strong">'
        f"{MEDICAL_ADVICE_NOTICE}.</span></div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="cl-notice">{VERIFICATION_NOTE}</div>',
        unsafe_allow_html=True,
    )


def render_hero(result: Optional[PipelineResult]) -> None:
    """Hero header: name, promise, subtitle, and three status metric cards."""
    st.markdown(
        f'<div class="cl-hero">'
        f'<p class="cl-hero-name">{APP_NAME}</p>'
        f'<p class="cl-hero-promise">{APP_PROMISE}</p>'
        f'<p class="cl-hero-sub">{APP_SUBTITLE}</p>'
        f"</div>",
        unsafe_allow_html=True,
    )
    verified = len(result.verified_claims) if result else 0
    unsupported = len(result.unsupported_claims) if result else 0
    conflicts = len(result.conflict_flags) if result else 0
    cols = st.columns(3)
    for col, label, value in zip(
        cols, ("Verified", "Unsupported", "Conflicts"),
        (verified, unsupported, conflicts),
    ):
        with col:
            st.markdown(
                f'<div class="cl-metric"><p class="cl-metric-label">{label}</p>'
                f'<p class="cl-metric-value">{value}</p></div>',
                unsafe_allow_html=True,
            )


# --------------------------------------------------------------------------- #
# Session state                                                               #
# --------------------------------------------------------------------------- #

def _init_state() -> None:
    """Initialize session_state keys the UI relies on (idempotent)."""
    ss = st.session_state
    ss.setdefault("result", None)            # PipelineResult | None
    ss.setdefault("pending_hits", None)      # tuple[SearchHit, ...] | None
    ss.setdefault("pending_symbol", None)    # the symbol being disambiguated
    ss.setdefault("gene_text", "")           # the text input value
    ss.setdefault("tampered", {})            # claim_id -> tampered Claim (live edit)
    ss.setdefault("run_error", None)         # str | None


# --------------------------------------------------------------------------- #
# Sidebar: threshold controls (Req 9.6) + offline toggle                      #
# --------------------------------------------------------------------------- #

def render_sidebar() -> tuple[Thresholds, bool]:
    """Render the four conflict thresholds and the offline toggle.

    Each control shows its current value; defaults come from
    ``Config().thresholds``. Returns the chosen :class:`Thresholds` and the
    offline flag. Changing any control triggers a Streamlit rerun, and the
    conflicts are re-evaluated immediately because the next run/render uses these
    values (Req 9.7).
    """
    defaults = Config().thresholds
    st.sidebar.header("Thresholds")
    st.sidebar.caption("Conflict rules re-evaluate as you change these.")

    support = st.sidebar.slider(
        "Support_Threshold",
        min_value=0.0, max_value=1.0, value=float(defaults.support_threshold),
        step=0.05,
        help="A datatype counts as supporting when its score is at or above this.",
    )
    min_dt = st.sidebar.slider(
        "Minimum_Supporting_Datatype_Count",
        min_value=1, max_value=6, value=int(defaults.min_supporting_datatypes),
        step=1,
        help="Fewer supporting datatypes than this flags Single_Source_Dependence.",
    )
    high_score = st.sidebar.slider(
        "High_Score_Threshold",
        min_value=0.0, max_value=1.0, value=float(defaults.high_score_threshold),
        step=0.05,
        help="An association at or above this score is 'high' for Literature_Gap.",
    )
    low_co = st.sidebar.number_input(
        "Low_Co_Mention_Threshold",
        min_value=0, max_value=100000, value=int(defaults.low_co_mention_threshold),
        step=10,
        help="A co-mention count at or below this is 'low' for Literature_Gap.",
    )

    st.sidebar.markdown("---")
    offline = st.sidebar.toggle(
        "Offline (cached only)",
        value=True,
        help="Serve the example genes from the committed cache with no network.",
    )
    st.sidebar.caption(
        "Current: support "
        f"{support:.2f}, min datatypes {int(min_dt)}, high score {high_score:.2f}, "
        f"low co-mention {int(low_co)}."
    )

    return (
        Thresholds(
            support_threshold=float(support),
            min_supporting_datatypes=int(min_dt),
            high_score_threshold=float(high_score),
            low_co_mention_threshold=int(low_co),
        ),
        bool(offline),
    )


# --------------------------------------------------------------------------- #
# Run orchestration (UI-side)                                                 #
# --------------------------------------------------------------------------- #

def _run_symbol(symbol: str, thresholds: Thresholds, offline: bool) -> None:
    """Resolve + run ``symbol`` and store the outcome in session_state.

    Branches on the resolution status: RESOLVED stores the full result;
    DISAMBIGUATE stores the hits for the user to choose (Req 1.6); NOT_FOUND
    records an error message (Req 1.5). Clears any stale tamper edits on a new
    run.
    """
    ss = st.session_state
    ss["run_error"] = None
    ss["pending_hits"] = None
    ss["pending_symbol"] = None
    ss["tampered"] = {}

    pipeline = get_pipeline(thresholds, offline)
    try:
        outcome = pipeline.resolve(symbol)
    except Exception as exc:  # defensive: never crash the page on a fetch error
        ss["result"] = None
        ss["run_error"] = f"Could not resolve '{symbol}': {exc}"
        return

    if outcome.status is ResolveStatus.NOT_FOUND:
        ss["result"] = None
        ss["run_error"] = (
            f"'{symbol}' could not be resolved to an Open Targets target. "
            "Check the symbol, or try an example gene."
        )
        return
    if outcome.status is ResolveStatus.DISAMBIGUATE:
        ss["result"] = None
        ss["pending_hits"] = outcome.hits
        ss["pending_symbol"] = symbol
        return

    # RESOLVED
    assert outcome.ensembl_id is not None
    ss["result"] = pipeline.run_resolved(
        outcome.ensembl_id,
        resolved_symbol=outcome.resolved_symbol or symbol,
        resolve_outcome=outcome,
        requested_symbol=symbol,
    )


def _run_chosen_hit(hit, thresholds: Thresholds, offline: bool) -> None:
    """Resolve a user-chosen disambiguation hit and run it (Req 1.7)."""
    ss = st.session_state
    pipeline = get_pipeline(thresholds, offline)
    outcome = pipeline.resolve_chosen(hit)
    ss["pending_hits"] = None
    ss["pending_symbol"] = None
    ss["tampered"] = {}
    ss["result"] = pipeline.run_resolved(
        outcome.ensembl_id,
        resolved_symbol=outcome.resolved_symbol or hit.name,
        resolve_outcome=outcome,
        requested_symbol=hit.name,
    )


# --------------------------------------------------------------------------- #
# Input section                                                               #
# --------------------------------------------------------------------------- #

def render_input(thresholds: Thresholds, offline: bool) -> None:
    """Gene text input + Analyze button + four example buttons (Req 1.1, 1.4)."""
    st.markdown("### Analyze a gene")
    col_in, col_btn = st.columns([4, 1])
    with col_in:
        gene_text = st.text_input(
            "Gene symbol",
            key="gene_text",
            placeholder="e.g. EGFR",
            label_visibility="collapsed",
        )
    with col_btn:
        analyze = st.button("Analyze", type="primary", use_container_width=True)

    if analyze:
        ok, cleaned, message = validate_gene_input(gene_text)
        if not ok:
            st.warning(message)  # Req 1.4: no retrieval issued for empty input
        else:
            _run_symbol(cleaned, thresholds, offline)

    st.caption("Try an example")
    ex_cols = st.columns(len(EXAMPLE_GENES))
    for col, gene in zip(ex_cols, EXAMPLE_GENES):
        with col:
            if st.button(gene, key=f"ex_{gene}", use_container_width=True):
                st.session_state["gene_text"] = gene
                _run_symbol(gene, thresholds, offline)

    # Disambiguation list (Req 1.6): shown with NO auto-selection.
    ss = st.session_state
    if ss.get("pending_hits"):
        st.info(
            f"'{ss['pending_symbol']}' did not match a target by name. "
            "Choose which target to use:"
        )
        hits = ss["pending_hits"]
        labels = [f"{h.name}  ({h.id})" for h in hits]
        choice = st.radio(
            "Matching targets",
            options=list(range(len(hits))),
            format_func=lambda i: labels[i],
            index=None,  # no auto-selection (Req 1.6)
        )
        if choice is not None and st.button("Use this target"):
            _run_chosen_hit(hits[choice], thresholds, offline)

    if ss.get("run_error"):
        st.error(ss["run_error"])


# --------------------------------------------------------------------------- #
# Tabs                                                                        #
# --------------------------------------------------------------------------- #

def _all_claims(result: PipelineResult) -> list[Claim]:
    """Verified + Unsupported claims as one list (deterministic order)."""
    return list(result.verified_claims) + list(result.unsupported_claims)


def _display_claim(result: PipelineResult, claim: Claim) -> Claim:
    """Return the tampered version of a claim if one is live, else the claim."""
    return st.session_state.get("tampered", {}).get(claim.claim_id, claim)


def render_dossier_tab(result: PipelineResult) -> None:
    render_notices()
    st.markdown(f"#### Dossier for {result.gene_symbol}")
    if result.dossier_markdown.strip():
        st.markdown(result.dossier_markdown)
    else:
        st.info("No verified claims to render for this gene.")


def render_ledger_tab(result: PipelineResult) -> None:
    render_notices()
    st.markdown("#### Claim ledger")
    st.caption(
        "Every claim, verified or not. Status is shown as text and a symbol."
    )

    claims = [_display_claim(result, c) for c in _all_claims(result)]
    rows = []
    for c in claims:
        rows.append(
            "<tr>"
            f"<td>{c.claim_type.value}</td>"
            f"<td>{_escape(c.subject)}</td>"
            f'<td class="cl-num">{_escape(claim_value_display(c))}</td>'
            f"<td>{_status_badge_html(c.status)}</td>"
            f'<td class="cl-id">{_escape(", ".join(c.evidence_ids))}</td>'
            "</tr>"
        )
    table = (
        '<table class="cl-ledger"><thead><tr>'
        "<th>Type</th><th>Subject</th><th>Value</th><th>Status</th>"
        "<th>Evidence_ID</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )
    st.markdown(table, unsafe_allow_html=True)

    _render_tamper_control(result)


def _render_tamper_control(result: PipelineResult) -> None:
    """The Tamper/Reset demo card: alter one claim's value, watch it flip (Req 13)."""
    st.markdown("---")
    st.markdown(
        '<div class="cl-card"><strong>Tamper demonstration</strong><br>'
        '<span class="cl-hero-sub" style="margin-top:0.3rem;display:block;">'
        "Deliberately change one claim's number and re-run the verifier. It "
        "flips to Unsupported, with the claim value and the recomputed value "
        "shown. Reset restores it. The saved evidence is never changed."
        "</span></div>",
        unsafe_allow_html=True,
    )

    store = result.store
    if store is None:
        st.info("Tamper demo needs a live run; re-run a gene to enable it.")
        return

    claims = result.verified_claims
    numeric = [
        c for c in claims
        if c.claim_type in (
            ClaimType.ASSOCIATION,
            ClaimType.LITERATURE_COUNT,
            ClaimType.CO_MENTION_COUNT,
        )
    ]
    if not numeric:
        st.info("No numeric claim available to tamper with for this gene.")
        return

    labels = {
        f"{c.claim_type.value} - {c.subject}": c.claim_id for c in numeric
    }
    picked_label = st.selectbox("Pick a claim to tamper", list(labels))
    picked_id = labels[picked_label]
    picked = next(c for c in numeric if c.claim_id == picked_id)
    tampered_now = st.session_state["tampered"].get(picked_id)

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**Before**")
        st.markdown(
            f'{_status_badge_html(picked.status)} &nbsp; '
            f'<span class="cl-mono">{_escape(claim_value_display(picked))}</span>',
            unsafe_allow_html=True,
        )
    with col_b:
        st.markdown("**After**")
        if tampered_now is not None:
            st.markdown(
                f'{_status_badge_html(tampered_now.status)} &nbsp; '
                f'<span class="cl-mono">{_escape(claim_value_display(tampered_now))}</span>',
                unsafe_allow_html=True,
            )
            if tampered_now.reason:
                st.caption(f"Reason: {tampered_now.reason}")
        else:
            st.caption("Not tampered.")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Tamper (alter the value)", use_container_width=True):
            new_value = _perturbed_value(picked)
            tampered_claim, _result = tamper_apply(picked, new_value, store)
            st.session_state["tampered"][picked_id] = tampered_claim
            st.rerun()
    with c2:
        if st.button("Reset", use_container_width=True):
            base = tampered_now if tampered_now is not None else picked
            reset_claim, _result = tamper_reset(base, store)
            st.session_state["tampered"].pop(picked_id, None)
            st.rerun()


def _perturbed_value(claim: Claim) -> object:
    """A clearly-different value for the tamper demo (never equal to the truth)."""
    if claim.claim_type is ClaimType.ASSOCIATION:
        v = float(claim.value)
        return round(v + 0.5, 4) if v <= 0.5 else round(v - 0.5, 4)
    # counts
    try:
        return int(claim.value) + 1
    except (TypeError, ValueError):
        return 0


def render_conflicts_tab(result: PipelineResult) -> None:
    render_notices()
    st.markdown("#### Conflicts")
    st.caption(
        "Each flagged association is shown with both sides of its evidence "
        "side by side. Adjust thresholds in the sidebar to re-evaluate."
    )
    if not result.conflict_flags:
        st.success(
            "No conflicts flagged at the current thresholds. "
            "Status is also stated here as text, not color alone."
        )
        return

    co_mentions = result.co_mention_counts
    name_by_id = {a.disease_id: a.disease_name for a in result.associations}
    score_by_id = {a.disease_id: a.overall_score for a in result.associations}
    pair_ids = result.pair_evidence_ids or {}
    store = result.store

    for flag in result.conflict_flags:
        disease = name_by_id.get(flag.disease_id, flag.disease_id)
        st.markdown(
            f'<div class="cl-card"><span class="cl-rule-name">{flag.rule}</span>'
            f" &nbsp; {_escape(disease)}</div>",
            unsafe_allow_html=True,
        )
        if flag.rule == SINGLE_SOURCE_DEPENDENCE:
            counted = ", ".join(
                f"{ds.id} {float(ds.score):.3f}"
                for ds in sorted(flag.supporting_datatypes, key=lambda d: d.id)
            ) or "(none counted)"
            left = (
                "<h4>Supporting datatypes counted</h4>"
                f"<div class='cl-mono'>{_escape(counted)}</div>"
                f"<p class='cl-hero-sub'>{flag.supporting_count} datatype(s) at or "
                f"above the support threshold.</p>"
            )
            right = (
                "<h4>Rule</h4>"
                f"<p class='cl-hero-sub'>Fewer than the required minimum of "
                f"supporting datatypes. Overall evidence-strength score "
                f"{float(score_by_id.get(flag.disease_id, 0.0)):.4f}.</p>"
            )
        else:  # LITERATURE_GAP
            query, translation = _pair_query_strings(
                store, pair_ids.get(flag.disease_id)
            )
            left = (
                "<h4>Open Targets</h4>"
                f"<p class='cl-hero-sub'>Evidence-strength score "
                f"<strong>{float(flag.overall_score or 0.0):.4f}</strong> "
                "(high).</p>"
            )
            right = (
                "<h4>PubMed co-mentions</h4>"
                f"<p class='cl-hero-sub'>Count <strong>{flag.co_mention_count}</strong> "
                "(low).</p>"
                f"<div class='cl-mono'>query: {_escape(query)}</div>"
                f"<div class='cl-mono'>PubMed read it as: {_escape(translation)}</div>"
            )
        st.markdown(
            f'<div class="cl-sides"><div class="cl-side">{left}</div>'
            f'<div class="cl-side">{right}</div></div>',
            unsafe_allow_html=True,
        )


def _pair_query_strings(store, pair_evidence_id: Optional[str]) -> tuple[str, str]:
    """The submitted query + PubMed translation for a gene+disease pair record."""
    if store is None or not pair_evidence_id:
        return ("(unavailable)", "(unavailable)")
    rec = store.get(pair_evidence_id)
    if rec is None:
        return ("(unavailable)", "(unavailable)")
    return (rec.submitted_query or "(unavailable)", rec.query_translation or "(unavailable)")


def render_evidence_tab(result: PipelineResult) -> None:
    render_notices()
    st.markdown("#### Evidence")
    st.caption("For each claim: source, URL, when it was retrieved, and whether it came from cache.")

    store = result.store
    if store is None:
        st.info("Evidence provenance needs a live run; re-run a gene.")
        return

    for claim in _all_claims(result):
        with st.expander(f"{claim.claim_type.value} - {claim.subject}"):
            st.markdown(
                f'{_status_badge_html(claim.status)} &nbsp; '
                f'<span class="cl-mono">value {_escape(claim_value_display(claim))}</span>',
                unsafe_allow_html=True,
            )
            for eid in claim.evidence_ids:
                rec: Optional[EvidenceRecord] = store.get(eid)
                if rec is None:
                    st.markdown(
                        f'<div class="cl-mono">{_escape(eid)} - not in store</div>',
                        unsafe_allow_html=True,
                    )
                    continue
                cache_label = "from cache" if rec.from_cache else "live fetch"
                st.markdown(
                    f'<div class="cl-card">'
                    f'<div class="cl-mono">Evidence_ID: {_escape(eid)}</div>'
                    f"<div>Source: {rec.source.value}</div>"
                    f'<div class="cl-mono">URL: {_escape(rec.url)}</div>'
                    f"<div>Retrieved at: {_escape(rec.retrieved_at)}</div>"
                    f'<div><span class="cl-badge cl-badge-verified">{cache_label}</span></div>'
                    + (
                        f'<div class="cl-mono">query: {_escape(rec.submitted_query)}</div>'
                        if rec.submitted_query else ""
                    )
                    + (
                        f'<div class="cl-mono">translation: {_escape(rec.query_translation)}</div>'
                        if rec.query_translation else ""
                    )
                    + "</div>",
                    unsafe_allow_html=True,
                )


def render_why_tab(result: Optional[PipelineResult]) -> None:
    render_notices()
    st.markdown("#### Why this matters")
    st.markdown(
        "**Who benefits.** Researchers and curious readers who want to see the "
        "evidence behind a gene-disease claim, not just a number.\n\n"
        "**What the ledger is.** Every statement here is a structured claim tied "
        "to at least one piece of saved evidence. A verifier recomputes each "
        "number from its source; a claim is shown as Verified only when the "
        "number matches, and as Unsupported (with a reason) otherwise. Nothing "
        "is silently dropped.\n\n"
        "**Data sources.** Open Targets (gene-disease association scores and "
        "per-datatype breakdowns) and PubMed (literature and co-mention counts, "
        "and recent papers)."
    )
    st.markdown("##### How to read this")
    st.markdown(
        f'<div class="cl-legend-item">{_status_badge_html(ClaimStatus.VERIFIED)} '
        "&mdash; the number matches the value recomputed from its saved evidence."
        "</div>"
        f'<div class="cl-legend-item">{_status_badge_html(ClaimStatus.UNSUPPORTED)} '
        "&mdash; the number does not match its evidence (or the evidence is "
        "missing); the reason is shown.</div>"
        f'<div class="cl-legend-item"><span class="cl-rule-name">{SINGLE_SOURCE_DEPENDENCE}</span> '
        "&mdash; the association is supported by too few datatypes.</div>"
        f'<div class="cl-legend-item"><span class="cl-rule-name">{LITERATURE_GAP}</span> '
        "&mdash; a high association score with very little gene-plus-disease "
        "literature.</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "An Open Targets score is an evidence-strength score, not a probability."
    )

    if result is not None:
        _render_downloads(result)


def _render_downloads(result: PipelineResult) -> None:
    """Download buttons: claims as JSON, dossier as Markdown (Req 14.5)."""
    st.markdown("##### Downloads")
    exporter = Exporter()
    disease_names = {a.disease_id: a.disease_name for a in result.associations}
    claims_json = exporter.export_claims_json(
        result.verified_claims,
        result.unsupported_claims,
        gene_symbol=result.gene_symbol,
    )
    dossier_md = exporter.export_dossier_markdown(
        result.verified_claims,
        gene_symbol=result.gene_symbol,
        disease_names=disease_names,
    )
    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "Claims (JSON)",
            data=claims_json,
            file_name=f"{result.gene_symbol}_claims.json",
            mime="application/json",
            use_container_width=True,
        )
    with c2:
        st.download_button(
            "Dossier (Markdown)",
            data=dossier_md,
            file_name=f"{result.gene_symbol}_dossier.md",
            mime="text/markdown",
            use_container_width=True,
        )


def _escape(text: object) -> str:
    """Minimal HTML-escape for values interpolated into our markup."""
    s = str(text)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #

def main() -> None:
    """Render the whole page. Called by the streamlit_app.py entry point."""
    st.set_page_config(page_title=APP_NAME, page_icon=None, layout="centered")
    inject_css()
    _init_state()

    thresholds, offline = render_sidebar()
    render_hero(st.session_state.get("result"))
    st.markdown("---")
    render_input(thresholds, offline)

    result: Optional[PipelineResult] = st.session_state.get("result")
    st.markdown("---")
    tab_dossier, tab_ledger, tab_conflicts, tab_evidence, tab_why = st.tabs(
        ["Dossier", "Ledger", "Conflicts", "Evidence", "Why this matters"]
    )
    with tab_dossier:
        if result is not None:
            render_dossier_tab(result)
        else:
            render_notices()
            st.info("Analyze a gene or pick an example to see its dossier.")
    with tab_ledger:
        if result is not None:
            render_ledger_tab(result)
        else:
            render_notices()
            st.info("The claim ledger appears here after you analyze a gene.")
    with tab_conflicts:
        if result is not None:
            render_conflicts_tab(result)
        else:
            render_notices()
            st.info("Conflict checks appear here after you analyze a gene.")
    with tab_evidence:
        if result is not None:
            render_evidence_tab(result)
        else:
            render_notices()
            st.info("Evidence provenance appears here after you analyze a gene.")
    with tab_why:
        render_why_tab(result)


if __name__ == "__main__":
    main()
