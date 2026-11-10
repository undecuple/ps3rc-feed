"""`python3 -m collector ...` runs the command line in collector/cli.py."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
