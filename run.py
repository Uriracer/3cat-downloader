"""3Cat Downloader entry point for running from a source checkout and for PyInstaller.

All the start-up logic lives in :mod:`tresdl.cli` (also installed as the
``tresdl`` / ``3cat-downloader`` commands). See ``tresdl --help``.
"""
import sys

from tresdl.cli import main

if __name__ == "__main__":
    sys.exit(main())
