"""Pytest plugin used ONLY by scripts/mutation_check.py (never by a normal test run).

With THEMIS_DISABLE_HANDLER=<case> it replaces that table-parser handler with a no-op
before any test runs, so the runner can check that some test fails without it.
"""

import os

import pytest


def pytest_configure(config):
    case = os.environ.get("THEMIS_DISABLE_HANDLER")
    if not case:
        return
    from themis.extract import tables

    if case not in tables.HANDLERS:
        raise pytest.UsageError(f"no parser handler named {case!r}")
    tables.HANDLERS[case] = tables._noop
