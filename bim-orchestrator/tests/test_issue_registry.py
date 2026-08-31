"""Cross-run Path A issue registry (SPEC_SCHEDULED_AUDIT_DELTA.md W2a)."""

from __future__ import annotations

from pathlib import Path

from bim_orchestrator.issue_registry import IssueRegistry, group_key


class TestGroupKey:
    def test_stable_regardless_of_element_order(self) -> None:
        k1 = group_key("b.proj", "wall.fire_rating", "non_compliant", ["3", "1", "2"])
        k2 = group_key("b.proj", "wall.fire_rating", "non_compliant", ["1", "2", "3"])
        assert k1 == k2

    def test_different_project_differs(self) -> None:
        k1 = group_key("b.proj1", "wall.fire_rating", "non_compliant", ["1", "2"])
        k2 = group_key("b.proj2", "wall.fire_rating", "non_compliant", ["1", "2"])
        assert k1 != k2

    def test_different_rule_or_bucket_differs(self) -> None:
        base = group_key("b.proj", "rule.a", "non_compliant", ["1"])
        assert base != group_key("b.proj", "rule.b", "non_compliant", ["1"])
        assert base != group_key("b.proj", "rule.a", "manual_review", ["1"])

    def test_different_element_set_differs(self) -> None:
        k1 = group_key("b.proj", "rule.a", "non_compliant", ["1", "2"])
        k2 = group_key("b.proj", "rule.a", "non_compliant", ["1", "2", "3"])
        assert k1 != k2


class TestIssueRegistry:
    def test_lookup_missing_file_returns_none(self, tmp_path: Path) -> None:
        reg = IssueRegistry(tmp_path / "issue_registry.json")
        assert reg.lookup("nope") is None

    def test_lookup_corrupt_file_returns_none_no_raise(self, tmp_path: Path) -> None:
        p = tmp_path / "issue_registry.json"
        p.write_text("{not valid json", encoding="utf-8")
        reg = IssueRegistry(p)
        assert reg.lookup("anything") is None

    def test_lookup_malformed_shape_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "issue_registry.json"
        p.write_text('["just", "a", "list"]', encoding="utf-8")
        reg = IssueRegistry(p)
        assert reg.lookup("anything") is None

    def test_record_creates_file_and_parent_dir(self, tmp_path: Path) -> None:
        p = tmp_path / "nested" / "issue_registry.json"
        reg = IssueRegistry(p)
        reg.record("key1", {"issue_id": "issue-1"})
        assert p.exists()
        assert reg.lookup("key1") == {"issue_id": "issue-1"}

    def test_record_last_write_wins_on_same_key(self, tmp_path: Path) -> None:
        p = tmp_path / "issue_registry.json"
        reg = IssueRegistry(p)
        reg.record("key1", {"issue_id": "issue-1"})
        reg.record("key1", {"issue_id": "issue-2"})
        assert reg.lookup("key1") == {"issue_id": "issue-2"}

    def test_record_preserves_other_keys(self, tmp_path: Path) -> None:
        p = tmp_path / "issue_registry.json"
        reg = IssueRegistry(p)
        reg.record("key1", {"issue_id": "issue-1"})
        reg.record("key2", {"issue_id": "issue-2"})
        assert reg.lookup("key1") == {"issue_id": "issue-1"}
        assert reg.lookup("key2") == {"issue_id": "issue-2"}

    def test_new_instance_sees_previously_recorded_entry(self, tmp_path: Path) -> None:
        p = tmp_path / "issue_registry.json"
        IssueRegistry(p).record("key1", {"issue_id": "issue-1"})
        reg2 = IssueRegistry(p)
        assert reg2.lookup("key1") == {"issue_id": "issue-1"}
