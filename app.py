#!/usr/bin/env python3
"""Launch the desktop application."""

import sys


def _main() -> int:
    if "--selftest" in sys.argv:
        from anomizer.selftest import desktop_selftest

        return desktop_selftest()

    from anomizer.ui import run

    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
