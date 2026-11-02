"""``python -m tresdl`` — same as the ``tresdl`` command."""
import sys

from tresdl.cli import main

if __name__ == "__main__":
    sys.exit(main())
