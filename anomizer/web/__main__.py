"""Allow ``python -m anomizer.web``."""

from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
