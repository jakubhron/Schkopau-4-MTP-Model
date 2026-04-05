"""Allow running the package with ``python -m schkopau_mtp``."""

from __future__ import annotations

import sys
import os

# Ensure the project root is on the path when running as ``python -m schkopau_mtp``
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    """Entry point delegating to the top-level main module."""
    # Import here to avoid circular imports when the package is used as a library.
    from main import main as _run  # noqa: WPS433

    _run()


if __name__ == "__main__":
    main()
