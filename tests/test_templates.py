"""Every Jinja template must compile (a duplicated block 500s the dashboard)."""
from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


@pytest.mark.parametrize("name", sorted(str(p.relative_to(TEMPLATES)) for p in TEMPLATES.rglob("*.html")))
def test_template_compiles(name):
    Environment(loader=FileSystemLoader(str(TEMPLATES))).get_template(name)
