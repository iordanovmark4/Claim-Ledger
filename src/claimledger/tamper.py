"""Tamper / reset control operating only on ``Claim.value`` (Req 13).

This module is the small, pure core behind the UI's Tamper_Control (design
"Tamper / Reset Mechanism"). It demonstrates that the :class:`~claimledger.verify.Verifier`
genuinely re-checks numbers against saved evidence: deliberately altering one
displayed claim value makes verification reject it, and restoring the original
value makes it accept again.

Scope and invariants (Req 13.4, steering rule 8)
------------------------------------------------
Tamper and reset operate **purely** on a :class:`~claimledger.models.Claim`'s
``value`` field. They:

* never touch an :class:`~claimledger.models.EvidenceRecord`;
* never touch the :class:`~claimledger.evidence.EvidenceStore`;
* never touch the on-disk ``data/cache`` response bytes.

``Claim`` is a frozen dataclass, so every change is expressed as a **new** claim
produced with :func:`dataclasses.replace` — the input claim is itself never
mutated. Re-verification goes through the ordinary :class:`Verifier`, which is
pure and read-only over the store (so evidence is byte-identical before and
after by construction — nothing here can write to it).

``original_value`` holds the truth (Req 13.3)
----------------------------------------------
``original_value`` records the **pre-any-tamper** value so a reset restores the
genuine original, not an intermediate tamper. Therefore:

* the *first* tamper records the current (genuine) value into
  ``original_value``;
* a subsequent tamper on an already-tampered claim (``original_value`` already
  set) keeps that stored original and only replaces ``value`` — so a reset after
  two tampers restores the true original, never the first tamper's value;
* reset restores ``value = original_value`` and clears ``original_value`` back
  to ``None`` (the untampered state).
"""

from __future__ import annotations

from dataclasses import replace

from .evidence import EvidenceStore
from .models import Claim, VerificationResult
from .verify import Verifier, annotate


def tamper(
    claim: Claim,
    new_value: object,
    store: EvidenceStore,
    verifier: Verifier | None = None,
) -> tuple[Claim, VerificationResult]:
    """Alter ``claim``'s value to ``new_value`` and re-verify it (Req 13.1, 13.2, 13.4).

    Produces a **new** tampered claim (``Claim`` is frozen) whose ``value`` is
    ``new_value`` and whose ``original_value`` preserves the genuine pre-tamper
    value, then re-runs the :class:`Verifier` against the untouched ``store``.
    Because the altered value no longer equals the value recomputed from the
    unchanged evidence, the verdict is ``UNSUPPORTED`` with a reason reporting
    **both** the tampered value and the recomputed value (Req 13.2). The returned
    claim is :func:`~claimledger.verify.annotate`-d with that status/reason for
    the ledger (its ``value``/``original_value`` are left exactly as set here —
    annotate never repairs a value).

    ``original_value`` semantics (Req 13.3): the first tamper records the current
    genuine value; tampering an already-tampered claim (``original_value`` set)
    keeps the stored original so a later reset restores the truth, not an
    intermediate tamper.

    Immutability (Req 13.4): only ``Claim.value`` (and the bookkeeping
    ``original_value``) changes. The :class:`EvidenceStore` and the cached
    response bytes are not touched — verification is read-only over the store, so
    the referenced :class:`EvidenceRecord` is byte-identical before and after.

    Note: tamper does not *force* a rejection; it changes the value and
    re-verifies. If ``new_value`` happens to equal the recomputed value the
    verdict stays ``VERIFIED`` — but a genuine tamper supplies a distinct value.

    Returns the annotated tampered claim and its :class:`VerificationResult`.
    """
    verifier = verifier or Verifier()
    # Preserve the genuine pre-any-tamper value: record the current value as the
    # original only on the first tamper; keep an existing original otherwise.
    original = claim.value if claim.original_value is None else claim.original_value

    tampered = replace(claim, value=new_value, original_value=original)
    result = verifier.verify(tampered, store)
    return annotate(tampered, result), result


def reset(
    claim: Claim,
    store: EvidenceStore,
    verifier: Verifier | None = None,
) -> tuple[Claim, VerificationResult]:
    """Restore ``claim`` to its original value and re-verify it (Req 13.3, 13.4).

    Produces a **new** claim whose ``value`` is restored from ``original_value``
    and whose ``original_value`` is cleared to ``None`` (the untampered state),
    then re-runs the :class:`Verifier` against the untouched ``store``. For a
    claim that was tampered from a genuinely-verified value, the restored value
    matches its evidence again, so the verdict returns to ``VERIFIED``. The
    returned claim is :func:`~claimledger.verify.annotate`-d with that
    status/reason.

    Edge case (Req 13.3): resetting a claim that was never tampered
    (``original_value is None``) is a no-op on the value — the current value is
    kept and simply re-verified.

    Immutability (Req 13.4): only ``Claim.value``/``original_value`` change; the
    :class:`EvidenceStore` and cached response bytes are untouched.

    Returns the annotated reset claim and its :class:`VerificationResult`.
    """
    verifier = verifier or Verifier()
    # original_value None => never tampered: keep the current value as-is.
    restored = claim.value if claim.original_value is None else claim.original_value

    reset_claim = replace(claim, value=restored, original_value=None)
    result = verifier.verify(reset_claim, store)
    return annotate(reset_claim, result), result


__all__ = ["tamper", "reset"]
