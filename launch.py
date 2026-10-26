"""Portable entry point; uses the selected Python interpreter and this directory."""
from pulseatlas.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
