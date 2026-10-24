"""Import-only + pure-helper tests for the Streamlit UI module (task 18).

These never start a Streamlit server. They assert the module imports cleanly
without a Streamlit runtime, exposes the expected entry point and helpers, uses
the Config Example_Gene registry, carries the two mandatory notice strings, and
that the pure input validator behaves per Req 1.4.
"""

from __future__ import annotations

import claimledger.app as app
from claimledger.config import Config
from claimledger.render import SAFETY_NOTICE


def test_module_imports_and_exposes_entry_points() -> None:
    """The UI module imports with no Streamlit server and exposes main + helpers."""
    for name in (
        "main",
        "inject_css",
        "render_hero",
        "render_notices",
        "validate_gene_input",
        "get_pipeline",
        "claim_value_display",
    ):
        assert hasattr(app, name), f"app is missing {name}"
    assert callable(app.main)


def test_example_genes_match_config_registry() -> None:
    """The UI's example buttons use exactly the Config Example_Gene registry."""
    expected = tuple(
        g for g in Config().example_genes if isinstance(g, str) and g
    )
    assert app.EXAMPLE_GENES == expected
    # The four committed example genes.
    assert app.EXAMPLE_GENES == ("EGFR", "TP53", "BRCA1", "PANK2")


def test_mandatory_notice_strings_present() -> None:
    """Both mandatory notices exist (Req 10.4, 10.6)."""
    assert SAFETY_NOTICE in app.MEDICAL_ADVICE_NOTICE
    assert app.MEDICAL_ADVICE_NOTICE == "Evidence summary, not medical advice"
    # The verification note makes the "matches its source, not that it is true"
    # point (Req 10.6).
    assert "matches its saved source" in app.VERIFICATION_NOTE
    assert "not that the finding is true" in app.VERIFICATION_NOTE


def test_validate_gene_input_rejects_empty_and_whitespace() -> None:
    """Empty / whitespace-only input is invalid with a message (Req 1.4)."""
    for raw in ("", "   ", "\t\n"):
        ok, cleaned, message = app.validate_gene_input(raw)
        assert ok is False
        assert cleaned == ""
        assert message  # a user-facing message is provided


def test_validate_gene_input_accepts_and_strips_only() -> None:
    """A non-empty symbol is accepted and only stripped (not upper-cased)."""
    ok, cleaned, message = app.validate_gene_input("EGFR")
    assert ok is True
    assert cleaned == "EGFR"
    assert message is None

    # Surrounding whitespace is stripped; case is preserved (resolver is
    # case-insensitive, so the UI must not force-upper).
    ok, cleaned, message = app.validate_gene_input("  egfr ")
    assert ok is True
    assert cleaned == "egfr"
    assert message is None


def test_claim_value_display_formats_by_type() -> None:
    """The ledger value formatter is type-aware and deterministic."""
    from claimledger.models import Claim, ClaimType, DatatypeScore

    assoc = Claim(
        claim_id="ASSOCIATION:EGFR::D",
        claim_type=ClaimType.ASSOCIATION,
        subject="EGFR::D",
        value=0.8503239816603523,
        evidence_ids=("e1",),
    )
    assert app.claim_value_display(assoc) == "0.8503"

    count = Claim(
        claim_id="LITERATURE_COUNT:EGFR",
        claim_type=ClaimType.LITERATURE_COUNT,
        subject="EGFR",
        value=108097,
        evidence_ids=("e2",),
    )
    assert app.claim_value_display(count) == "108,097"

    breakdown = Claim(
        claim_id="DATATYPE_BREAKDOWN:EGFR::D",
        claim_type=ClaimType.DATATYPE_BREAKDOWN,
        subject="EGFR::D",
        value=(
            DatatypeScore(id="literature", score=0.9),
            DatatypeScore(id="clinical", score=0.8),
        ),
        evidence_ids=("e3",),
    )
    # Rendered sorted by id.
    assert app.claim_value_display(breakdown) == "clinical 0.800, literature 0.900"