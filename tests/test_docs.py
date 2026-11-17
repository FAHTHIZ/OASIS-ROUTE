"""Documentation stays in sync with the code: every setting and every public API path is documented."""

from __future__ import annotations

import re
from pathlib import Path

from oasis_route.app import create_app
from oasis_route.config import Settings

DOCS = Path(__file__).resolve().parents[1] / "docs"


def test_every_setting_is_documented() -> None:
    text = (DOCS / "CONFIGURATION.md").read_text()
    missing = [n for n in Settings.model_fields if f"OASIS_ROUTE_{n.upper()}" not in text]
    assert not missing, f"undocumented settings: {missing}"


def test_every_api_path_is_documented() -> None:
    def norm(path: str) -> str:
        return re.sub(r"\{[^}]+\}", "{}", path.removeprefix("/api/v1"))

    text = norm((DOCS / "API.md").read_text().replace("`/api/v1", "`"))
    paths = create_app(Settings()).openapi()["paths"]
    missing = [p for p in paths if not re.search(r"[` ]" + re.escape(norm(p)) + "`", text)]
    assert not missing, f"undocumented API paths: {missing}"


def test_internal_doc_links_resolve() -> None:
    root = DOCS.parent
    broken = []
    for md in [*root.glob("*.md"), *DOCS.glob("*.md")]:
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", md.read_text()):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (md.parent / target).exists():
                broken.append(f"{md.name} -> {target}")
    assert not broken, f"broken links: {broken}"
