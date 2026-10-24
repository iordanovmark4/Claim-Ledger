"""Streamlit Community Cloud / local entry point for Claim Ledger.

Run locally with::

    python -m streamlit run streamlit_app.py

On Streamlit Community Cloud this file is the app's main module; there is no
package install step, so we put the repo's ``src/`` directory on ``sys.path``
here (the project uses a ``src/`` layout) before importing the app. Keeping this
logic out of ``claimledger.app`` means ``import claimledger.app`` stays a clean,
side-effect-free import for the test suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from claimledger.app import main  # noqa: E402  (after sys.path setup)

main()
