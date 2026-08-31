"""Unit tests for the approval-integrity fingerprint (approval-security pass).

The fingerprint binds the human-approved ACC proposal issue to the exact set of
Revit writes the watcher will execute. These cover the pure hash/parse layer;
the watcher-side gate lives in test_approval_watcher.py.
"""

from __future__ import annotations

from bim_orchestrator.policies.approval_integrity import (
    fingerprint,
    fingerprint_line,
    parse_fingerprint,
)


def _fix(eid, param, new, *, action="set_parameter", **extra):
    return {"element_id": eid, "parameter": param, "new_value": new,
            "action": action, **extra}


def test_fingerprint_is_order_independent():
    a = [_fix("1", "Department", "Res"), _fix("2", "Comments", "Note")]
    b = list(reversed(a))
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_changes_when_a_write_changes():
    base = [_fix("1", "Department", "Res")]
    assert fingerprint(base) != fingerprint([_fix("1", "Department", "OTHER")])
    assert fingerprint(base) != fingerprint([_fix("9", "Department", "Res")])
    assert fingerprint(base) != fingerprint([_fix("1", "Comments", "Res")])
    assert fingerprint(base) != fingerprint([_fix("1", "Department", "Res",
                                                   action="rename_element")])


def test_fingerprint_ignores_cosmetic_fields():
    """finding_id / old_value / display sugar don't bind — only the write does."""
    a = [_fix("1", "Department", "Res", finding_id="r::1", old_value="")]
    b = [_fix("1", "Department", "Res", finding_id="DIFFERENT", old_value="stale")]
    assert fingerprint(a) == fingerprint(b)


def test_missing_action_defaults_to_set_parameter():
    """A record predating the `action` field hashes as a set_parameter."""
    with_action = [_fix("1", "Department", "Res", action="set_parameter")]
    without = [{"element_id": "1", "parameter": "Department", "new_value": "Res"}]
    assert fingerprint(with_action) == fingerprint(without)


def test_parse_fingerprint_roundtrips_from_issue_body():
    fp = fingerprint([_fix("1", "Department", "Res")])
    body = f"### Rule foo\nsome markdown\n\n---\n{fingerprint_line(fp)}\n"
    assert parse_fingerprint(body) == fp


def test_parse_fingerprint_absent_or_empty():
    assert parse_fingerprint(None) is None
    assert parse_fingerprint("a proposal with no marker") is None
