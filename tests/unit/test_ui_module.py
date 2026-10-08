import importlib

import pytest


def test_ui_module_imports_with_local_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSFLEET_ENV", "local")

    module = importlib.import_module("opsfleet_agent.ui.app")

    assert module.on_open_report is not None
