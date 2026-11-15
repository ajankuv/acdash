"""The app version is shown to users and must stay in step with CHANGELOG.md (the release
workflow refuses a tag whose version has no changelog section)."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

import app as app_pkg
from app.main import app, env

ROOT = Path(__file__).resolve().parents[1]


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", app_pkg.__version__)


def test_changelog_has_section_for_current_version():
    text = (ROOT / "CHANGELOG.md").read_text()
    assert re.search(rf"^## \[{re.escape(app_pkg.__version__)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", text, re.M)


def test_status_reports_version_and_revision():
    body = TestClient(app).get("/status").json()
    assert body["version"] == app_pkg.__version__
    assert body["revision"] == app_pkg.REVISION


def test_footer_shows_version():
    html = env.get_template("setup.html").render(error=None)
    assert f"AC Dash v{app_pkg.__version__}" in html


def test_revision_from_build_arg(monkeypatch):
    import importlib

    monkeypatch.setenv("ACDASH_REVISION", "38d7822abcdef0123456789")
    try:
        assert importlib.reload(app_pkg).REVISION == "38d7822"
        monkeypatch.setenv("ACDASH_REVISION", "  ")
        assert importlib.reload(app_pkg).REVISION == "dev"
    finally:
        monkeypatch.delenv("ACDASH_REVISION")
        importlib.reload(app_pkg)
