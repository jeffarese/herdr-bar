#!/usr/bin/env python3
"""Entry point for the Herdr bar.

Herdr launches manifest commands with the plugin directory as the working
directory, so this shim only has to put ``src`` on the import path.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

if __name__ == "__main__":
    if sys.argv[1:] == ["--open"]:
        # The Windows open action only runs the Herdr CLI; skip the bar's imports.
        from herdr_bar.opener import open_bar
        sys.exit(open_bar())
    from herdr_bar.cli import main
    sys.exit(main(sys.argv[1:]))
