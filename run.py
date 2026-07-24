#!/usr/bin/env python3
"""Entry point for the Herdr bar.

Herdr launches manifest commands with the plugin directory as the working
directory, so this shim only has to put ``src`` on the import path.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from herdr_bar.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
