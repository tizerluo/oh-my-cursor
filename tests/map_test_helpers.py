#!/usr/bin/env python3
"""Shared helpers for MAP / review_gate tests."""

from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[1] / "hooks"


def load_review_gate():
    spec = importlib.util.spec_from_file_location("review_gate", HOOKS_DIR / "review_gate.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SecretBootstrapMixin:
    """Isolate tests from host secret / OMC_SECRET_FILE (R02 fail-closed)."""

    def _bootstrap_secret(self, rg) -> None:
        self._secret_tmp = tempfile.TemporaryDirectory()
        secret = Path(self._secret_tmp.name) / "secret"
        rg.bootstrap_secret(secret)
        os.environ["OMC_SECRET_FILE"] = str(secret)

    def _clear_secret_env(self) -> None:
        os.environ.pop("OMC_SECRET_FILE", None)
        if hasattr(self, "_secret_tmp"):
            self._secret_tmp.cleanup()
            del self._secret_tmp
