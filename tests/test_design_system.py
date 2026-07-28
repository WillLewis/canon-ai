"""Design-system conformance (P3-POLISH) — all offline.

DESIGN.md is law: one light Continuity Desk skin on every surface. Two layers
of enforcement here:

1. Route walk: every GET route in the app renders 200-or-4xx; every HTML page
   links the token stylesheet and carries no legacy dark-workbench chrome.
2. Static audits over ui/templates/*.html + ui/static/style.css: no gradients,
   no border-radius above 2px, red pencil tokens only on finding/severity/
   caught elements, and the three faces self-hosted with font-display: swap.

    python -m pytest -q tests/test_design_system.py
"""

import contextlib
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ui import db  # noqa: E402
from ui import rules_ui, share_ui  # noqa: E402
from ui.app import app  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
TEMPLATES = sorted((REPO / "ui" / "templates").glob("*.html"))
STYLE = REPO / "ui" / "static" / "style.css"
FONTS = REPO / "ui" / "static" / "fonts"

WORLD = {"id": 1, "name": "greyharbor_s1"}
SUMMARY = {
    "totals": {"entities": 0, "assertions": 0, "scenes": 0, "works": 0,
               "findings_live": 0, "findings_sealed": 0},
    "entities_by_kind": [], "assertions_by_status": [],
    "findings_by_sev": [], "works": [],
}

# Path-parameter fill-ins for the route walk. Anything numeric defaults to 1.
PARAM_VALUES = {"token": "nosuchtoken", "note_id": "n-1"}

# Legacy chrome that must never render again: the old dark-workbench tokens
# and its branded topbar.
LEGACY_MARKERS = ("--header", "#1b2230", "Workbench", 'class="surface"')


@contextlib.contextmanager
def wired():
    """TestClient over the real app with every db/store touchpoint faked to
    empty-but-well-formed data, so read views render and auth'd routes 401."""
    saved_env = os.environ.pop("AUTH_DISABLED", None)
    db_fakes = {
        "list_worlds": lambda: [dict(WORLD)],
        "member_role": lambda world_id, user_id: None,
        "world_summary": lambda world_id: SUMMARY,
        "list_entities": lambda world_id, kind=None, q=None: [],
        "kinds_in_world": lambda world_id: [],
        "get_entity": lambda world_id, entity_id: None,
        "list_assertions": lambda world_id, **kw: ([], 0),
        "predicates_in_world": lambda world_id: [],
        "get_assertion": lambda world_id, assertion_id: None,
        "list_scenes": lambda world_id: [],
        "get_scene": lambda world_id, scene_id: None,
        "list_findings_composed": lambda world_id: [],
        "get_finding": lambda world_id, finding_id: None,
        "list_coverage_notes": lambda world_id: [],
        "entity_names": lambda world_id, ids: {},
        "load_bearing": lambda world_id, limit=6: [],
        "list_scenes_with_text": lambda world_id: [],
        "list_draft_assertions": lambda world_id: [],
        "last_story_position": lambda world_id: None,
        # P3-FRONTDOOR touchpoints (theater/poll/upload routes)
        "get_run": lambda run_id: None,
        "latest_run_for_world": lambda world_id: None,
        "list_run_events": lambda run_id, after=0: [],
        "get_world": lambda name: None,
    }
    rules_fakes = {
        "store_list": lambda world_id: [],
        "world_traits": lambda world_id: [],
        "world_positions": lambda world_id: [],
    }
    saved_db = {n: getattr(db, n) for n in db_fakes}
    saved_rules = {n: getattr(rules_ui, n) for n in rules_fakes}
    saved_share = {
        "cursor_ctx": share_ui.cursor_ctx,
        "resolve": share_ui.share_store.resolve_share_link,
    }
    try:
        for n, fn in db_fakes.items():
            setattr(db, n, fn)
        for n, fn in rules_fakes.items():
            setattr(rules_ui, n, fn)
        share_ui.cursor_ctx = contextlib.nullcontext
        share_ui.share_store.resolve_share_link = lambda cur, token: None
        yield TestClient(app, follow_redirects=False)
    finally:
        if saved_env is not None:
            os.environ["AUTH_DISABLED"] = saved_env
        for n, fn in saved_db.items():
            setattr(db, n, fn)
        for n, fn in saved_rules.items():
            setattr(rules_ui, n, fn)
        share_ui.cursor_ctx = saved_share["cursor_ctx"]
        share_ui.share_store.resolve_share_link = saved_share["resolve"]


def _get_paths():
    paths = []
    for route in app.routes:
        if isinstance(route, APIRoute) and "GET" in route.methods:
            path = re.sub(r"\{(\w+)[^}]*\}",
                          lambda m: PARAM_VALUES.get(m.group(1), "1"),
                          route.path)
            paths.append(path)
    return sorted(set(paths))


# --- 1 · route walk -------------------------------------------------------------


def test_every_get_route_renders_on_the_token_system():
    paths = _get_paths()
    assert paths, "route walk found no GET routes"
    html_pages = 0
    with wired() as client:
        for path in paths:
            r = client.get(path)
            assert 200 <= r.status_code < 500, f"{path} -> {r.status_code}"
            if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
                html_pages += 1
                body = r.text
                assert '/static/style.css' in body, f"{path}: token stylesheet not linked"
                for marker in LEGACY_MARKERS:
                    assert marker not in body, f"{path}: legacy chrome {marker!r} rendered"
    assert html_pages >= 10, f"route walk only rendered {html_pages} HTML pages"


def test_every_template_rides_the_one_base_chrome():
    """One chrome, one stylesheet: every page template either extends base.html
    or (standalone error pages) links the token stylesheet itself."""
    for t in TEMPLATES:
        if t.name.startswith("_"):
            continue  # macro fragments
        text = t.read_text()
        assert ('extends "base.html"' in text) or ('/static/style.css' in text), (
            f"{t.name}: neither extends base.html nor links the token stylesheet")


# --- 2 · static audits ----------------------------------------------------------


def _sources():
    yield STYLE, STYLE.read_text()
    for t in TEMPLATES:
        yield t, t.read_text()


def test_no_gradients_anywhere():
    for path, text in _sources():
        assert "gradient(" not in text, f"{path.name}: gradient found (DESIGN.md: zero gradients)"


def test_border_radius_never_exceeds_2px():
    ok = re.compile(r"^\s*(0|[0-2](\.\d+)?px)\s*$")
    for path, text in _sources():
        for m in re.finditer(r"border-radius\s*:\s*([^;}\"']+)", text):
            for token in m.group(1).split():
                assert ok.match(token), (
                    f"{path.name}: border-radius {m.group(1).strip()!r} — documents are not rounded (max 2px)")


def test_red_pencil_only_where_canon_caught_something():
    """--redpencil/--pressed may style finding/severity/caught elements only —
    never nav, buttons, or decoration."""
    red = re.compile(r"--redpencil|--pressed|#C43B22|#A81F11", re.I)
    # Templates carry no red at all (styling lives in the stylesheet) — with one
    # law-compliant exception: landing.html's hero renders a CAUGHT contradiction
    # (the red rule, grease-pencil circle, and margin note ARE the catch).
    for t in TEMPLATES:
        if t.name == "landing.html":
            continue
        assert not red.search(t.read_text()), f"{t.name}: red token in a template"
    css = STYLE.read_text()
    allowed = (":root", ".card.has-crit", ".sev-critical", ".check")
    for selector_block in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selectors, body = selector_block.groups()
        if not red.search(body):
            continue
        assert any(a in selectors for a in allowed), (
            f"red pencil styled on {selectors.strip()!r} — allowed only on "
            f"finding/severity/caught elements {allowed}")
        for banned in ("btn", "nav", "topbar", "brand", "foot", "pill"):
            assert banned not in selectors, (
                f"red pencil styled on {selectors.strip()!r} — red is never a button/nav color")


def test_legacy_dark_tokens_are_gone_from_the_stylesheet():
    css = STYLE.read_text()
    for marker in ("--header", "#1b2230", "--accent", "--panel", "--crit", "--warn-bg"):
        assert marker not in css, f"style.css still carries legacy token {marker!r}"


def test_fonts_are_self_hosted_with_swap():
    css = STYLE.read_text()
    faces = re.findall(r"@font-face\s*\{([^}]*)\}", css)
    assert len(faces) == 5, "expected 5 @font-face blocks (Fraunces, Instrument Sans, Courier Prime x3)"
    for face in faces:
        assert "font-display: swap" in face
        src = re.search(r'url\("(/static/fonts/[^"]+\.woff2)"\)', face)
        assert src, "font src must be self-hosted under /static/fonts/"
        assert (REPO / "ui" / "static" / src.group(1).removeprefix("/static/")
                ).exists(), f"missing font file {src.group(1)}"
    for family in ("Fraunces", "Instrument Sans", "Courier Prime"):
        assert f'font-family: "{family}"' in css
    assert (FONTS / "LICENSE.md").exists(), "font license note must ship with the binaries"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
