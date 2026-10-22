"""Shared helpers for the platform-backend tests (run on Windows, macOS and Linux).

No __init__.py on purpose: this directory is called "platform", and as a
package it would shadow the stdlib module.
"""
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[2] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


def fake_which(*present: str):
    """shutil.which stand-in: only the named programs exist (at /usr/bin/<name>)."""
    def which(name: str):
        return f"/usr/bin/{name}" if name in present else None
    return which


@pytest.fixture
def which_factory():
    return fake_which
