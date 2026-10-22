"""PyInstaller entry point (PyInstaller needs a script, not `-m pandadeck`)."""
import multiprocessing
import sys

from pandadeck.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
