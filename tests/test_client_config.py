"""Tests for app/client.py module-level configuration."""
import importlib

import app.client as client_mod


def _reload(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ACINFINITY_API_BASE", raising=False)
    else:
        monkeypatch.setenv("ACINFINITY_API_BASE", value)
    return importlib.reload(client_mod)


def test_api_base_default_when_unset(monkeypatch):
    mod = _reload(monkeypatch, None)
    assert mod.API_BASE == "http://www.acinfinityserver.com/api"
    assert mod.LOGIN_ENDPOINT == "http://www.acinfinityserver.com/api/user/appUserLogin"
    assert mod.ADD_DEV_MODE_ENDPOINT == "http://www.acinfinityserver.com/api/dev/addDevMode"


def test_api_base_default_when_blank(monkeypatch):
    mod = _reload(monkeypatch, "   ")
    assert mod.API_BASE == "http://www.acinfinityserver.com/api"


def test_api_base_override_applies_to_all_endpoints(monkeypatch):
    mod = _reload(monkeypatch, "http://fake:9000/api/")
    assert mod.API_BASE == "http://fake:9000/api"
    endpoints = [v for k, v in vars(mod).items() if k.endswith("_ENDPOINT") and isinstance(v, str)]
    assert endpoints, "expected endpoint constants"
    assert all(e.startswith("http://fake:9000/api/") for e in endpoints)


def teardown_module(_module):
    # Leave the module in its default state for other test files.
    import os
    os.environ.pop("ACINFINITY_API_BASE", None)
    importlib.reload(client_mod)
