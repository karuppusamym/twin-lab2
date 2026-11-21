"""The recorder's completeness dimensions (contract 1.8, completeness.dimensions)."""

from twinlab2_recorder import ui_model as ui
from twinlab2_recorder.workstation import completeness

MANIFEST = {"overall": "partial", "network_evidence": "incomplete", "channels": {
    "web": {"status": "complete", "gaps": [], "notes": []},
    "desktop": {"status": "interrupted", "gaps": [], "notes": []},
    "mainframe": {"status": "not_requested", "gaps": [], "notes": []},
    "network": {"status": "interrupted", "gaps": [], "notes": []}}}
FULL = {"web:https://a.test"}


def row(eid, app="web:https://a.test", incomplete=False):
    step = {"seq": 1, "event_id": eid}
    if incomplete:
        step["incomplete"] = True
    return {"app": app, "event_id": eid, "_step": step}


def test_each_dimension_on_its_own():
    rows = [row("e1"), row("e2"), row("e3", incomplete=True), row("d1", app="desktop:x/y"),
            {"app": "presence", "action": "presence"}]
    screens = {"web:https://a.test": [{"screen": "Orders", "states": [{"id": "s1", "events": ["e1", "e2"]}]}]}
    got = completeness.dimensions(MANIFEST, rows, screens, FULL)
    assert got["channels"]["status"] == "partial" and (got["channels"]["have"], got["channels"]["of"]) == (1, 3)
    assert got["event_detail"]["status"] == "partial" and (got["event_detail"]["have"], got["event_detail"]["of"]) == (3, 4)
    assert got["screen_coverage"]["status"] == "partial"
    assert (got["screen_coverage"]["have"], got["screen_coverage"]["of"]) == (2, 3)  # the desktop app is not full
    assert got["network_evidence"]["status"] == "partial"


def test_nothing_recorded():
    got = completeness.dimensions(MANIFEST, [], {}, set())
    assert got["event_detail"]["status"] == "not_applicable"
    assert got["screen_coverage"]["status"] == "not_applicable"


def test_no_state_at_all_is_none():
    got = completeness.dimensions(MANIFEST, [row("e1"), row("e2")], None, FULL)
    assert got["screen_coverage"]["status"] == "none"


def test_the_send_review_lists_the_dimensions():
    box = {"session": {"id": "s"}, "completeness": dict(MANIFEST, dimensions=completeness.dimensions(
        MANIFEST, [row("e1")], None, FULL))}
    text = ui.review_summary(box)
    for title in ("Channel availability", "Event detail", "Screen and state coverage", "Network evidence"):
        assert title in text
    old = ui.review_summary({"session": {"id": "s"}, "completeness": MANIFEST})  # an older outbox
    assert "Event detail: unknown" in old
