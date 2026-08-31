"""Reusable test doubles for the Forma MCP client.

`MockFormaMCPClient` implements the same async interface as `FormaMCPClient`
but holds in-memory state — no subprocess, no network. Use it to:

    * Unit-test individual agents (QueryAgent, DesignAgent)
    * Run full-graph integration tests deterministically
    * Simulate failure modes (no subtypes, all inactive, API error)

The mock records every call so tests can assert on the exact sequence.

Also exposes ``make_test_ruleset()`` — a compact constructor for tiny
``RuleSet`` instances used in v1.3+ tests where the query agents take a
``RuleSet`` instead of a category string.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Self

from bim_orchestrator.policies.rules_schema import Rule, RuleAutofill, RuleSet

# LIVE PROBE 2026-07-12 (addin v0.8.13, F1): characters Revit forbids in a
# view/schedule name. MockRevitMCPClient.create_schedule uses this to mirror
# the addin's silent fallback-to-default behaviour (F1/F3) instead of
# honouring a name the real addin would quietly refuse.
_REVIT_FORBIDDEN_VIEW_NAME_CHARS = set("[]{}|;<>?~:\\")


def make_test_ruleset(
    *,
    target_category: str | list[str],
    parameter: str = "Department",
    rule_id: str = "test.rule",
    requirement: str = "present_and_nonempty",
    category: str | None = None,
    when_param: str | None = None,
    other_param: str | None = None,
    scenario: str = "test_scenario",
    extra_rules: list[Rule] | None = None,
) -> RuleSet:
    """One-rule RuleSet for v1.3 query-agent tests.

    The default rule (``present_and_nonempty`` on ``Department``) is a
    cheap placeholder — most tests assert on which categories were
    fetched, not on QC behaviour. Pass ``extra_rules`` when you need
    multiple rules (host hop, multi-param, etc.).
    """
    base = Rule(
        id=rule_id,
        parameter=parameter,
        requirement=requirement,  # type: ignore[arg-type]
        category=category,
        when_param=when_param,
        other_param=other_param,
        severity_tag="quality_change",
        description=f"{rule_id} test placeholder",
        autofill=RuleAutofill(strategy="none"),
    )
    return RuleSet(
        scenario=scenario,
        target_category=target_category,
        rules=[base, *(extra_rules or [])],
    )

# ---- Realistic ACC sample data --------------------------------------------

# Mirrors the shape returned by aecdm_query_elements on Sample ACC Project,
# with the 30-rooms sample (Closet 11A, Bathroom 1 07, etc).
# Realistic Walls fixture for Phase 2 Week 4 Day 4 fire-rating E2E.
# Mix of valid / missing / wrong-format FireRating to exercise all rule branches.
SAMPLE_WALLS: list[dict[str, Any]] = [
    {
        "id": "wall-corridor-101",
        "name": "Wall WAL-101 (Corridor)",
        "properties": [
            {"name": "Name", "value": "Corridor Wall 101"},
            {"name": "Function", "value": "Corridor"},
            {"name": "FireRating", "value": None},       # MISSING — fires required rule
            {"name": "AssemblyType", "value": "Type X GWB on metal stud"},
        ],
    },
    {
        "id": "wall-corridor-102",
        "name": "Wall WAL-102 (Corridor)",
        "properties": [
            {"name": "Name", "value": "Corridor Wall 102"},
            {"name": "Function", "value": "Corridor"},
            {"name": "FireRating", "value": "120 min"},  # WRONG FORMAT — fires regex rule
            {"name": "AssemblyType", "value": "Type X GWB on metal stud"},
        ],
    },
    {
        "id": "wall-corridor-103",
        "name": "Wall WAL-103 (Corridor)",
        "properties": [
            {"name": "Name", "value": "Corridor Wall 103"},
            {"name": "Function", "value": "Corridor"},
            {"name": "FireRating", "value": "2-hour"},   # VALID
            {"name": "AssemblyType", "value": "Type X GWB on metal stud"},
        ],
    },
    {
        "id": "wall-interior-201",
        "name": "Wall WAL-201 (Interior)",
        "properties": [
            {"name": "Name", "value": "Interior Partition 201"},
            {"name": "Function", "value": "Interior"},
            {"name": "FireRating", "value": ""},          # MISSING (empty string)
            {"name": "AssemblyType", "value": None},      # MISSING (soft rule fires too)
        ],
    },
    {
        "id": "wall-shaft-301",
        "name": "Wall WAL-301 (Shaft)",
        "properties": [
            {"name": "Name", "value": "Elevator Shaft 301"},
            {"name": "Function", "value": "Shaft"},
            {"name": "FireRating", "value": "2-hour"},   # VALID
            {"name": "AssemblyType", "value": "Concrete shaft wall"},
        ],
    },
]


SAMPLE_ROOMS: list[dict[str, Any]] = [
    {
        "id": "elem-closet-11a",
        "name": "Closet 11A",
        "properties": [
            {"name": "Name", "value": "Closet"},
            {"name": "Number", "value": "11A"},
            {"name": "Department", "value": None},
            {"name": "Occupancy", "value": None},
            {"name": "Area", "value": 0.83},
            {"name": "Comments", "value": "4'-6\"W x 2'-0\"L"},
            {"name": "Family Name", "value": "Closet 11A"},
        ],
    },
    {
        "id": "elem-bathroom-1-07",
        "name": "Bathroom 1 07",
        "properties": [
            {"name": "Name", "value": "Bathroom 1"},
            {"name": "Number", "value": "07"},
            {"name": "Department", "value": ""},
            {"name": "Occupancy", "value": ""},
            {"name": "Area", "value": 5.04},
        ],
    },
    {
        "id": "elem-entry-01",
        "name": "Entry 01",
        "properties": [
            {"name": "Name", "value": "Entry"},
            {"name": "Number", "value": "01"},
            {"name": "Department", "value": None},
            {"name": "Occupancy", "value": None},
            {"name": "Area", "value": 4.5},
        ],
    },
]

# v1.3: list_elements(OST_Rooms) shape — same ids/names as SAMPLE_REVIT_ROOMS
# so the per-id element_info lookups (SAMPLE_REVIT_ELEMENT_INFO) still resolve.
# Includes the level / metric convenience fields the real Revit MCP attaches
# to room instances at the list level (the unified RevitQueryAgent reads
# them when available + falls back to Area/Unbounded Height conversion).
SAMPLE_REVIT_ROOMS_AS_ELEMENTS: list[dict[str, Any]] = [
    {
        "id": 829712, "name": "Studio Unit 203", "category": "Rooms",
        "categoryEnum": "OST_Rooms", "typeId": -1,
        "levelName": "L2", "areaMetric": 55.08, "perimeter": 108.84,
    },
    {
        "id": 830966, "name": "Storage P04", "category": "Rooms",
        "categoryEnum": "OST_Rooms", "typeId": -1,
        "levelName": "Parking", "areaMetric": 5.32, "perimeter": 30.99,
    },
    {
        "id": 829648, "name": "Corridor 201", "category": "Rooms",
        "categoryEnum": "OST_Rooms", "typeId": -1,
        "levelName": "L2", "areaMetric": 55.38, "perimeter": 210.67,
    },
    {
        "id": 999001, "name": "Bedroom 999", "category": "Rooms",
        "categoryEnum": "OST_Rooms", "typeId": -1,
        "levelName": "L2", "areaMetric": 8.50, "perimeter": 38.00,
    },
    {
        "id": 999002, "name": "Duplicate Studio 203", "category": "Rooms",
        "categoryEnum": "OST_Rooms", "typeId": -1,
        "levelName": "L4", "areaMetric": 55.74, "perimeter": 110.00,
    },
]


SAMPLE_SUBTYPES: list[dict[str, Any]] = [
    {"id": "subtype-design-inactive", "title": "Design", "type_id": "type-design",
     "type_title": "Design", "is_active": False},
    {"id": "subtype-design-req-change", "title": "Requirement Change",
     "type_id": "type-design", "type_title": "Design", "is_active": False},
    {"id": "subtype-quality", "title": "Quality", "type_id": "type-quality",
     "type_title": "Quality", "is_active": True},
    {"id": "subtype-general", "title": "General", "type_id": "type-general",
     "type_title": "General", "is_active": True},
]


# ---- The mock client -------------------------------------------------------

@dataclass
class MockFormaMCPClient:
    """In-memory FormaMCPClient stand-in.

    Pass `elements`, `subtypes`, `fail_on` to customize per-test. Use:
        async with MockFormaMCPClient(elements=[...]) as client: ...
    OR directly (no context manager — `__aenter__` just returns self).

    Category dispatch: when `elements_by_category` is set, the mock returns
    different element lists per requested category. Useful for E2E tests
    that exercise both Rooms and Walls in the same suite.
    """

    elements: list[dict[str, Any]] = field(default_factory=lambda: list(SAMPLE_ROOMS))
    elements_by_category: dict[str, list[dict[str, Any]]] | None = None
    subtypes: list[dict[str, Any]] = field(default_factory=lambda: list(SAMPLE_SUBTYPES))
    fail_on: set[str] = field(default_factory=set)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # v1.4-K5: in-memory issue store for list/get/update/add_comment.
    issues: list[dict[str, Any]] = field(default_factory=list)
    # M10 mock parity: aecdm navigation fixtures (hubs → projects → element
    # groups) used by the Streamlit Setup tab's browse flow. Empty by default
    # — per-test fixture, no default project hierarchy assumed.
    hubs: list[dict[str, Any]] = field(default_factory=list)
    projects_by_hub: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    element_groups_by_project: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # element_id -> properties dict, for get_element_properties.
    element_properties: dict[str, dict[str, Any]] = field(default_factory=dict)

    _token_counter: int = 0
    _issue_counter: int = 0
    _audit_chain: list[dict[str, Any]] = field(default_factory=list)
    _comments: dict[str, list[str]] = field(default_factory=dict)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    # --- low-level call interface ----------------------------------------

    async def call_structured(
        self, tool: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        args = arguments or {}
        self.calls.append((tool, args))
        if tool in self.fail_on:
            raise RuntimeError(f"Simulated failure: MCP tool {tool}")
        if tool == "aecdm_list_hubs":
            return {"hubs": list(self.hubs)}
        if tool == "aecdm_list_projects":
            hub_id = args.get("hub_id")
            return {"projects": list(self.projects_by_hub.get(hub_id, []))}
        if tool == "aecdm_list_element_groups":
            project_id = args.get("project_id")
            return {"element_groups": list(self.element_groups_by_project.get(project_id, []))}
        if tool == "aecdm_get_element_properties":
            element_id = args.get("element_id")
            return {"properties": self.element_properties.get(element_id, {})}
        if tool == "aecdm_query_elements":
            category = args.get("category")
            # Per-category dispatch wins over the default `elements` list
            if self.elements_by_category is not None:
                elements = self.elements_by_category.get(category, [])
            else:
                elements = list(self.elements)
            return {"elements": elements, "category": category}
        if tool == "issues_list_types":
            return {"types": _group_subtypes_by_type(self.subtypes)}
        if tool == "issues_create":
            return self._handle_issues_create(args)
        if tool == "issues_list":
            issues = list(self.issues)
            assigned_to = args.get("assigned_to")
            if assigned_to is not None:
                # Mock issue fixtures carry no `assignedTo`/`assigned_to` field,
                # so an assignee filter never matches anything in-memory —
                # honor the param (don't silently drop it) by returning empty
                # rather than pretending every issue matches.
                issues = [
                    i for i in issues
                    if i.get("assignedTo") == assigned_to or i.get("assigned_to") == assigned_to
                ]
            return {"issues": issues, "pagination": {
                "limit": args.get("limit"), "offset": args.get("offset", 0)}}
        if tool == "issues_get":
            iid = args.get("issue_id")
            found = next((i for i in self.issues if i.get("id") == iid), None)
            if found is None:
                raise RuntimeError(f"issue {iid} not found")
            return {"issue": {
                "permittedStatuses": ["open", "in_progress", "closed"],
                **found,
            }}
        if tool == "issues_update":
            return self._handle_issues_update(args)
        if tool == "issues_add_comment":
            return self._handle_issues_add_comment(args)
        if tool == "meta_verify_audit_chain":
            return {
                "valid": True,
                "date": "2026-05-21",
                "entryCount": len(self._audit_chain),
                "firstEntryId": self._audit_chain[0]["id"] if self._audit_chain else None,
            }
        raise NotImplementedError(f"MockFormaMCPClient does not support tool: {tool}")

    async def call(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        # Legacy call returning content (text). Not used in Phase 1 critical path.
        await self.call_structured(tool, arguments)
        return []

    # --- typed convenience wrappers (match real client) ------------------

    async def list_aecdm_hubs(self) -> list[dict[str, Any]]:
        structured = await self.call_structured("aecdm_list_hubs", {})
        return list(structured.get("hubs", []))

    async def list_aecdm_projects(self, hub_id: str) -> list[dict[str, Any]]:
        structured = await self.call_structured(
            "aecdm_list_projects", {"hub_id": hub_id}
        )
        return list(structured.get("projects", []))

    async def list_element_groups(self, project_id: str) -> list[dict[str, Any]]:
        structured = await self.call_structured(
            "aecdm_list_element_groups", {"project_id": project_id}
        )
        return list(structured.get("element_groups", []))

    async def get_element_properties(
        self, element_group_id: str, element_id: str, category: str
    ) -> dict[str, Any]:
        structured = await self.call_structured(
            "aecdm_get_element_properties",
            {
                "element_group_id": element_group_id,
                "element_id": element_id,
                "category": category,
            },
        )
        return structured.get("properties", {})

    async def verify_audit_chain(self, *, since: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {}
        if since is not None:
            args["since"] = since
        return await self.call_structured("meta_verify_audit_chain", args)

    async def query_elements(
        self, element_group_id: str, category: str
    ) -> list[dict[str, Any]]:
        structured = await self.call_structured(
            "aecdm_query_elements",
            {"element_group_id": element_group_id, "category": category},
        )
        return list(structured.get("elements", []))

    async def list_issue_subtypes(self, project_id: str) -> list[dict[str, Any]]:
        structured = await self.call_structured(
            "issues_list_types", {"project_id": project_id}
        )
        flat: list[dict[str, Any]] = []
        for t in structured.get("types", []):
            for s in t.get("subtypes", []):
                flat.append(
                    {
                        "id": s.get("id"),
                        "title": s.get("title"),
                        "type_id": t.get("id"),
                        "type_title": t.get("title"),
                        "is_active": bool(s.get("isActive", True)),
                    }
                )
        return flat

    async def create_issue(
        self,
        project_id: str,
        *,
        title: str,
        issue_subtype_id: str,
        description: str | None = None,
        assigned_to: str | None = None,
        assigned_to_type: str | None = None,
        published: bool = False,
        status: str = "open",
        dry_run: bool = True,
        approval_token: str | None = None,
        linked_documents: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        args = {
            "project_id": project_id,
            "title": title,
            "issue_subtype_id": issue_subtype_id,
            "description": description,
            "published": published,
            "status": status,
            "dry_run": dry_run,
            "approval_token": approval_token,
            # v1 task B: record but tolerate None so existing tests keep
            # asserting against the original arg shape via `args["title"]`.
            "linked_documents": linked_documents,
        }
        self.calls.append(("create_issue", args))
        return self._handle_issues_create({**args, "tool_kind": "wrapper"})

    # --- v1.4-K5 issue read/update wrappers (match real client) ----------

    async def list_issues(
        self, project_id: str, *, status: str | None = None,
        assigned_to: str | None = None, limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"project_id": project_id, "limit": limit, "offset": offset}
        if status is not None:
            args["status"] = status
        if assigned_to is not None:
            args["assigned_to"] = assigned_to
        return await self.call_structured("issues_list", args)

    async def get_issue(self, project_id: str, issue_id: str) -> dict[str, Any]:
        return await self.call_structured(
            "issues_get", {"project_id": project_id, "issue_id": issue_id}
        )

    async def update_issue(
        self, project_id: str, issue_id: str, *, status: str | None = None,
        title: str | None = None, description: str | None = None,
        dry_run: bool = True, approval_token: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "project_id": project_id, "issue_id": issue_id, "dry_run": dry_run,
        }
        if status is not None:
            args["status"] = status
        if title is not None:
            args["title"] = title
        if description is not None:
            args["description"] = description
        if approval_token is not None:
            args["approval_token"] = approval_token
        return await self.call_structured("issues_update", args)

    async def add_issue_comment(
        self, project_id: str, issue_id: str, body: str, *,
        dry_run: bool = True, approval_token: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "project_id": project_id, "issue_id": issue_id, "body": body,
            "dry_run": dry_run,
        }
        if approval_token is not None:
            args["approval_token"] = approval_token
        return await self.call_structured("issues_add_comment", args)

    # --- internal helpers ------------------------------------------------

    def _handle_issues_update(self, args: dict[str, Any]) -> dict[str, Any]:
        iid = args.get("issue_id")
        if args.get("dry_run", True):
            self._token_counter += 1
            return {
                "approval_token": f"appr_upd_{self._token_counter:04d}",
                "preview": {"issue_id": iid, "status": args.get("status")},
            }
        if not args.get("approval_token"):
            raise RuntimeError("execute requires approval_token")
        issue = next((i for i in self.issues if i.get("id") == iid), None)
        if issue is None:
            raise RuntimeError(f"issue {iid} not found")
        if args.get("status") is not None:
            issue["status"] = args["status"]
        if args.get("title") is not None:
            issue["title"] = args["title"]
        return {"issue": dict(issue)}

    def _handle_issues_add_comment(self, args: dict[str, Any]) -> dict[str, Any]:
        iid = args.get("issue_id")
        if args.get("dry_run", True):
            self._token_counter += 1
            return {"approval_token": f"appr_cmt_{self._token_counter:04d}"}
        if not args.get("approval_token"):
            raise RuntimeError("execute requires approval_token")
        self._comments.setdefault(iid, []).append(args.get("body", ""))
        return {"comment": {"issue_id": iid, "body": args.get("body")}}

    def _handle_issues_create(self, args: dict[str, Any]) -> dict[str, Any]:
        # Validate inactive subtype like the real server's business rule
        subtype_id = args.get("issue_subtype_id")
        subtype = next((s for s in self.subtypes if s["id"] == subtype_id), None)
        if subtype and not subtype.get("is_active", True):
            raise RuntimeError(
                f'Business rule "issue_subtype_must_be_active" failed: '
                f'issue_subtype_id "{subtype_id}" ("{subtype["title"]}") is inactive.'
            )

        if args.get("dry_run", True):
            self._token_counter += 1
            token = f"appr_mock_{self._token_counter:04d}"
            self._audit_chain.append({
                "id": f"evt_preview_{self._token_counter:04d}",
                "tool": "issues_create",
                "stage": "preview",
            })
            return {
                "approval_token": token,
                "method": "POST",
                "url": f"https://mock/projects/{args['project_id']}/issues",
                "body": {"title": args.get("title"), "issueSubtypeId": subtype_id},
                "sideEffects": [f"Create 1 issue titled '{args.get('title')}'"],
                "businessRulesPassed": ["issue_subtype_id_exists_in_project"],
            }

        if not args.get("approval_token"):
            raise RuntimeError("execute requires approval_token")
        self._issue_counter += 1
        issue_id = f"issue-mock-{self._issue_counter:04d}"
        self._audit_chain.append({
            "id": f"evt_exec_{self._issue_counter:04d}",
            "tool": "issues_create",
            "stage": "executed",
        })
        issue = {
            "id": issue_id,
            "displayId": 1000 + self._issue_counter,
            "title": args.get("title"),
            "description": args.get("description"),
            "status": args.get("status", "open"),
        }
        # v1.4-K5: keep created issues in the store so list/get find them.
        self.issues.append(issue)
        return {"issue": dict(issue)}

    # --- test helpers ----------------------------------------------------

    def calls_to(self, tool: str) -> list[dict[str, Any]]:
        return [args for name, args in self.calls if name == tool]

    def execute_calls(self) -> list[dict[str, Any]]:
        """Just the create_issue calls that were actual executes (dry_run=False)."""
        return [
            args for name, args in self.calls
            if name == "create_issue" and args.get("dry_run") is False
        ]


def _group_subtypes_by_type(flat: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-group flat subtypes back into the {types: [{subtypes: [...]}]} shape."""
    by_type: dict[str, dict[str, Any]] = {}
    for s in flat:
        tid = s["type_id"]
        if tid not in by_type:
            by_type[tid] = {"id": tid, "title": s["type_title"], "subtypes": []}
        by_type[tid]["subtypes"].append(
            {"id": s["id"], "title": s["title"], "isActive": s.get("is_active", True)}
        )
    return list(by_type.values())


# ============================================================================
# Revit MCP — Phase 2 Week 6
# ============================================================================
#
# Fixtures derived from R26_Snowdon Towers Sample Architectural.rvt. Coordinates
# (bounding box) are in **feet** (Revit internal units). `area` is ft²,
# `areaMetric` is m² (mirrors the live revit_list_rooms response shape).
#
# Includes deliberate violators for room-compliance rules:
#   * Storage P04 — area 5.3 m² < 9 m², bbox width 1.86 m < 2.4 m
#   * Mock Bedroom 999 — area 8.5 m² < 10 m² bedroom minimum
#   * Two rooms share Number "203" — exercises unique_in_set evaluator
# ----------------------------------------------------------------------------


# `list_rooms`-shaped fixture (legacy bulk schedule format). Kept for the
# CLI smoke command + any tests that still exercise that tool. v1.3+
# RevitQueryAgent queries OST_Rooms via list_elements instead — see
# SAMPLE_REVIT_ROOMS_AS_ELEMENTS below for the matching shape.
SAMPLE_REVIT_ROOMS: list[dict[str, Any]] = [
    {
        "id": 829712,
        "name": "Studio Unit 203",
        "number": "203",
        "levelId": 593177,
        "levelName": "L2",
        "area": 592.88,
        "areaMetric": 55.08,
        "perimeter": 108.84,
        "department": "",
    },
    {
        "id": 830966,
        "name": "Storage P04",
        "number": "P04",
        "levelId": 612792,
        "levelName": "Parking",
        "area": 57.30,
        "areaMetric": 5.32,
        "perimeter": 30.99,
        "department": "",
    },
    {
        "id": 829648,
        "name": "Corridor 201",
        "number": "201",
        "levelId": 593177,
        "levelName": "L2",
        "area": 596.08,
        "areaMetric": 55.38,
        "perimeter": 210.67,
        "department": "",
    },
    {
        "id": 999001,
        "name": "Bedroom 999",
        "number": "999",
        "levelId": 593177,
        "levelName": "L2",
        "area": 91.50,
        "areaMetric": 8.50,
        "perimeter": 38.00,
        "department": "Residential",
    },
    {
        "id": 999002,
        "name": "Duplicate Studio 203",
        "number": "203",  # collides with id 829712 — fires uniqueness rule
        "levelId": 593178,
        "levelName": "L4",
        "area": 600.00,
        "areaMetric": 55.74,
        "perimeter": 110.00,
        "department": "",
    },
]


# Full `get_element_info` payloads for the rooms above. Indexed by id.
# Parameters list mirrors the live response: each entry has name, value,
# valueString, storageType. We carry just the fields the orchestrator reads.
SAMPLE_REVIT_ELEMENT_INFO: dict[int, dict[str, Any]] = {
    829712: {
        "id": 829712,
        "name": "Studio Unit 203",
        "category": "Rooms",
        "levelId": 593177,
        "boundingBox": {
            "min": {"x": -39.114, "y": 9.260, "z": 8.083},
            "max": {"x": -20.740, "y": 41.698, "z": 18.333},
        },
        "parameters": [
            {"name": "Name", "value": "Studio Unit", "valueString": "Studio Unit"},
            {"name": "Number", "value": "203", "valueString": "203"},
            {"name": "Department", "value": "", "valueString": ""},
            {
                "name": "Occupancy",
                "value": "Residential One Story",
                "valueString": "Residential One Story",
            },
            {"name": "Area", "value": 592.88, "valueString": "593 SF"},
            {"name": "Unbounded Height", "value": 10.25, "valueString": "10' - 3\""},
            {"name": "Comments", "value": None, "valueString": None},
        ],
    },
    830966: {
        "id": 830966,
        "name": "Storage P04",
        "category": "Rooms",
        "levelId": 612792,
        "boundingBox": {
            "min": {"x": -5.448, "y": 34.651, "z": -16.917},
            "max": {"x": 3.948, "y": 40.750, "z": -6.917},
        },
        "parameters": [
            {"name": "Name", "value": "Storage", "valueString": "Storage"},
            {"name": "Number", "value": "P04", "valueString": "P04"},
            {"name": "Department", "value": "", "valueString": ""},
            {"name": "Occupancy", "value": "", "valueString": ""},
            {"name": "Area", "value": 57.30, "valueString": "57 SF"},
            {"name": "Unbounded Height", "value": 10.0, "valueString": "10' - 0\""},
            {"name": "Comments", "value": None, "valueString": None},
        ],
    },
    829648: {
        "id": 829648,
        "name": "Corridor 201",
        "category": "Rooms",
        "levelId": 593177,
        "boundingBox": {
            "min": {"x": -39.365, "y": 2.500, "z": 8.083},
            "max": {"x": 47.604, "y": 18.615, "z": 18.333},
        },
        "parameters": [
            {"name": "Name", "value": "Corridor", "valueString": "Corridor"},
            {"name": "Number", "value": "201", "valueString": "201"},
            {"name": "Department", "value": "", "valueString": ""},
            {"name": "Occupancy", "value": "", "valueString": ""},
            {"name": "Area", "value": 596.08, "valueString": "596 SF"},
            {"name": "Unbounded Height", "value": 10.25, "valueString": "10' - 3\""},
            {"name": "Comments", "value": None, "valueString": None},
        ],
    },
    999001: {
        "id": 999001,
        "name": "Bedroom 999",
        "category": "Rooms",
        "levelId": 593177,
        "boundingBox": {
            "min": {"x": 0.0, "y": 0.0, "z": 8.083},
            "max": {"x": 10.0, "y": 9.15, "z": 16.0},  # 9.15 ft = ~2.79 m width
        },
        "parameters": [
            {"name": "Name", "value": "Bedroom", "valueString": "Bedroom"},
            {"name": "Number", "value": "999", "valueString": "999"},
            {
                "name": "Department",
                "value": "Residential",
                "valueString": "Residential",
            },
            {"name": "Occupancy", "value": "Residential", "valueString": "Residential"},
            {"name": "Area", "value": 91.50, "valueString": "92 SF"},
            {"name": "Unbounded Height", "value": 8.5, "valueString": "8' - 6\""},
            {"name": "Comments", "value": None, "valueString": None},
        ],
    },
    999002: {
        "id": 999002,
        "name": "Duplicate Studio 203",
        "category": "Rooms",
        "levelId": 593178,
        "boundingBox": {
            "min": {"x": 100.0, "y": 100.0, "z": 32.0},
            "max": {"x": 120.0, "y": 130.0, "z": 42.0},
        },
        "parameters": [
            {"name": "Name", "value": "Studio Unit", "valueString": "Studio Unit"},
            {"name": "Number", "value": "203", "valueString": "203"},
            {"name": "Department", "value": "", "valueString": ""},
            {
                "name": "Occupancy",
                "value": "Residential One Story",
                "valueString": "Residential One Story",
            },
            {"name": "Area", "value": 600.0, "valueString": "600 SF"},
            {"name": "Unbounded Height", "value": 10.25, "valueString": "10' - 3\""},
            {"name": "Comments", "value": None, "valueString": None},
        ],
    },
}


# ----------------------------------------------------------------------------
# Phase 2 Week 7 Day 1 — Fire-rating fixtures
#
# 4 walls (3 distinct types — one missing Fire Rating)
# 4 doors (2 distinct types — one rated "180 MIN", one "NR")
# Door hosts are explicit so the QueryAgent's host 2-hop is exercised.
# Expected fire-rating violations (against the rules YAML written next):
#   * wall.type.fire_rating.required → 1  (wall #102 type P01 has "")
#   * door.fire_rating.matches_host  → 2  (door #200 hosted in 4HR wall but
#                                          rated only 180MIN; door #201 NR in
#                                          4HR wall)
# ----------------------------------------------------------------------------

# Wall types — keyed by typeId. Sampling pattern matches Snowdon's Basic Wall.
SAMPLE_WALL_TYPES: dict[int, dict[str, Any]] = {
    1000: {
        "id": 1000,
        "name": "Core - Concrete 12\"",
        "category": "Walls",
        "categoryEnum": "OST_Walls",
        "typeId": -1,
        "parameters": [
            {"name": "Family Name", "value": "Basic Wall"},
            {"name": "Type Name", "value": "Core - Concrete 12\""},
            {"name": "Type Mark", "value": "C02"},
            {"name": "Fire Rating", "value": "4 HR", "valueString": "4 HR"},
            {"name": "Function", "value": 5, "valueString": "Core-shaft"},
            {"name": "Width", "value": 1.0, "valueString": "305"},
        ],
    },
    1001: {
        "id": 1001,
        "name": "Exterior - 13 5/8\" Rainscreen",
        "category": "Walls",
        "categoryEnum": "OST_Walls",
        "typeId": -1,
        "parameters": [
            {"name": "Family Name", "value": "Basic Wall"},
            {"name": "Type Name", "value": "Exterior - 13 5/8\" Rainscreen"},
            {"name": "Type Mark", "value": "X06"},
            {"name": "Fire Rating", "value": "2 HR", "valueString": "2 HR"},
            {"name": "Function", "value": 1, "valueString": "Exterior"},
            {"name": "Width", "value": 1.135, "valueString": "346"},
        ],
    },
    1002: {
        "id": 1002,
        "name": "Interior - Partition",
        "category": "Walls",
        "categoryEnum": "OST_Walls",
        "typeId": -1,
        "parameters": [
            {"name": "Family Name", "value": "Basic Wall"},
            {"name": "Type Name", "value": "Interior - Partition"},
            {"name": "Type Mark", "value": "P01"},
            {"name": "Fire Rating", "value": "", "valueString": ""},
            {"name": "Function", "value": 0, "valueString": "Interior"},
            {"name": "Width", "value": 0.354, "valueString": "108"},
        ],
    },
}

# Door types — keyed by typeId.
SAMPLE_DOOR_TYPES: dict[int, dict[str, Any]] = {
    2000: {
        "id": 2000,
        "name": "36\" x 84\" (180 MIN)",
        "category": "Doors",
        "categoryEnum": "OST_Doors",
        "typeId": -1,
        "parameters": [
            {"name": "Family Name", "value": "Door-Passage-Single-Flush"},
            {"name": "Type Name", "value": "36\" x 84\" (180 MIN)"},
            {"name": "Type Mark", "value": "77"},
            {"name": "Fire Rating", "value": "180 MIN", "valueString": "180 MIN"},
            {"name": "Width", "value": 3.0, "valueString": "914"},
        ],
    },
    2001: {
        "id": 2001,
        "name": "36\" x 84\"",
        "category": "Doors",
        "categoryEnum": "OST_Doors",
        "typeId": -1,
        "parameters": [
            {"name": "Family Name", "value": "Door-Passage-Single-Flush"},
            {"name": "Type Name", "value": "36\" x 84\""},
            {"name": "Type Mark", "value": "11"},
            {"name": "Fire Rating", "value": "NR", "valueString": "NR"},
            {"name": "Width", "value": 3.0, "valueString": "914"},
        ],
    },
}

# Wall instances. revit_list_elements returns these shapes.
SAMPLE_REVIT_WALLS: list[dict[str, Any]] = [
    {"id": 100, "name": "Core - Concrete 12\"", "category": "Walls",
     "categoryEnum": "OST_Walls", "typeId": 1000},
    {"id": 101, "name": "Exterior - 13 5/8\" Rainscreen", "category": "Walls",
     "categoryEnum": "OST_Walls", "typeId": 1001},
    {"id": 102, "name": "Interior - Partition", "category": "Walls",
     "categoryEnum": "OST_Walls", "typeId": 1002},
    {"id": 103, "name": "Core - Concrete 12\"", "category": "Walls",
     "categoryEnum": "OST_Walls", "typeId": 1000},
]

# Door instances. Each has a Host Id (instance param) pointing to a wall id.
SAMPLE_REVIT_DOORS: list[dict[str, Any]] = [
    {"id": 200, "name": "36\" x 84\" (180 MIN)", "category": "Doors",
     "categoryEnum": "OST_Doors", "typeId": 2000},
    {"id": 201, "name": "36\" x 84\"", "category": "Doors",
     "categoryEnum": "OST_Doors", "typeId": 2001},
    {"id": 202, "name": "36\" x 84\" (180 MIN)", "category": "Doors",
     "categoryEnum": "OST_Doors", "typeId": 2000},
    {"id": 203, "name": "36\" x 84\"", "category": "Doors",
     "categoryEnum": "OST_Doors", "typeId": 2001},
]

# Wall instance info — minimal, mostly just identity.
# Walls don't carry Fire Rating at instance level; rules read from the type.
_WALL_INSTANCE_PARAMS = lambda type_id: [  # noqa: E731
    {"name": "Type Id", "value": type_id, "valueString": str(type_id)},
    {"name": "Mark", "value": None, "valueString": None},
    {"name": "Comments", "value": None, "valueString": None},
]

# Door instance info — adds the critical "Host Id" pointing to a wall.
_DOOR_INSTANCE_PARAMS = lambda type_id, host_id: [  # noqa: E731
    {"name": "Type Id", "value": type_id, "valueString": str(type_id)},
    {"name": "Host Id", "value": host_id, "valueString": str(host_id)},
    {"name": "Mark", "value": None, "valueString": None},
    {"name": "Comments", "value": None, "valueString": None},
]

# Wall instance + door instance get_element_info responses.
SAMPLE_WALL_DOOR_INSTANCE_INFO: dict[int, dict[str, Any]] = {
    # Walls
    100: {"id": 100, "name": "Core - Concrete 12\"", "category": "Walls",
          "categoryEnum": "OST_Walls", "typeId": 1000,
          "parameters": _WALL_INSTANCE_PARAMS(1000)},
    101: {"id": 101, "name": "Exterior - 13 5/8\" Rainscreen", "category": "Walls",
          "categoryEnum": "OST_Walls", "typeId": 1001,
          "parameters": _WALL_INSTANCE_PARAMS(1001)},
    102: {"id": 102, "name": "Interior - Partition", "category": "Walls",
          "categoryEnum": "OST_Walls", "typeId": 1002,
          "parameters": _WALL_INSTANCE_PARAMS(1002)},
    103: {"id": 103, "name": "Core - Concrete 12\"", "category": "Walls",
          "categoryEnum": "OST_Walls", "typeId": 1000,
          "parameters": _WALL_INSTANCE_PARAMS(1000)},
    # Doors — Host Id ties each to its host wall above.
    200: {"id": 200, "name": "36\" x 84\" (180 MIN)", "category": "Doors",
          "categoryEnum": "OST_Doors", "typeId": 2000,
          "parameters": _DOOR_INSTANCE_PARAMS(2000, host_id=100)},
    201: {"id": 201, "name": "36\" x 84\"", "category": "Doors",
          "categoryEnum": "OST_Doors", "typeId": 2001,
          "parameters": _DOOR_INSTANCE_PARAMS(2001, host_id=100)},
    202: {"id": 202, "name": "36\" x 84\" (180 MIN)", "category": "Doors",
          "categoryEnum": "OST_Doors", "typeId": 2000,
          "parameters": _DOOR_INSTANCE_PARAMS(2000, host_id=101)},
    203: {"id": 203, "name": "36\" x 84\"", "category": "Doors",
          "categoryEnum": "OST_Doors", "typeId": 2001,
          "parameters": _DOOR_INSTANCE_PARAMS(2001, host_id=102)},
}

# Unified "get any element id" map for tests that want walls + doors + types
# accessible from a single element_info dict.
SAMPLE_FIRE_RATING_ELEMENT_INFO: dict[int, dict[str, Any]] = {
    **SAMPLE_WALL_DOOR_INSTANCE_INFO,
    **SAMPLE_WALL_TYPES,
    **SAMPLE_DOOR_TYPES,
}


# Drawing sheets — the shape revit_list_sheets returns ({count, sheets: [...]}).
# Field names live-verified on R27: id / sheetNumber / name / viewportCount
# (+ titleBlockId, omitted here). Includes deliberate violators for the
# sheet-compliance rules:
#   * A-102 appears twice (id 301 + 302) — fires Sheet Number unique_in_set
#   * id 304 has a blank `name` — fires Sheet Name present_and_nonempty
SAMPLE_REVIT_SHEETS: list[dict[str, Any]] = [
    {"id": 300, "sheetNumber": "A-101", "name": "Floor Plan", "viewportCount": 2},
    {"id": 301, "sheetNumber": "A-102", "name": "Elevations", "viewportCount": 1},
    {"id": 302, "sheetNumber": "A-102", "name": "Sections", "viewportCount": 1},  # dup number
    {"id": 303, "sheetNumber": "A-103", "name": "Details", "viewportCount": 1},
    {"id": 304, "sheetNumber": "A-104", "name": "", "viewportCount": 0},          # missing name
]


@dataclass
class MockRevitMCPClient:
    """In-memory RevitMCPClient stand-in.

    Pass ``rooms`` and ``element_info`` to customize per-test. Tracks every
    call as (tool, args) so tests can assert on dispatch.

    Writes (``set_parameter``, ``rename_element``) update the in-memory
    ``parameters`` list when ``dry_run=False`` so re-reads see the change.

    Both ``rooms`` and ``element_info`` are deep-copied per-instance so
    parallel tests can't bleed state into each other via the shared
    SAMPLE_REVIT_* module-level fixtures.
    """

    rooms: list[dict[str, Any]] = field(
        default_factory=lambda: copy.deepcopy(SAMPLE_REVIT_ROOMS)
    )
    element_info: dict[int, dict[str, Any]] = field(
        default_factory=lambda: copy.deepcopy(SAMPLE_REVIT_ELEMENT_INFO)
    )
    # Phase 2 W7 D1 — bulk list responses keyed by BuiltInCategory.
    # Tests can override per-instance; default covers Snowdon-like
    # walls + doors so the fire-rating QueryAgent has something to chew on
    # without each test having to set this up.
    elements_by_category: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {
            "OST_Walls": copy.deepcopy(SAMPLE_REVIT_WALLS),
            "OST_Doors": copy.deepcopy(SAMPLE_REVIT_DOORS),
            # v1.3: unified RevitQueryAgent queries Rooms via list_elements too.
            "OST_Rooms": copy.deepcopy(SAMPLE_REVIT_ROOMS_AS_ELEMENTS),
        }
    )
    # v1.4-J: geometry check fixtures.
    # linked_files — list of link records returned by revit_get_linked_files.
    # clearance_violations — list of violation dicts returned by revit_check_clearance.
    # active_view — dict returned by revit_get_active_view (None → returns {}).
    # all_views — list returned by revit_get_views.
    linked_files: list[dict[str, Any]] = field(default_factory=list)
    clearance_violations: list[dict[str, Any]] = field(default_factory=list)
    families: list[dict[str, Any]] = field(default_factory=list)
    active_view: dict[str, Any] | None = None
    all_views: list[dict[str, Any]] = field(default_factory=list)
    # v1.4-K3: MEP Spaces for containment enrichment. Each space's bbox lives
    # in element_info (so get_element_geometry resolves it like any element).
    spaces: list[dict[str, Any]] = field(default_factory=list)
    # Levels for revit_list_levels (M10 mock parity). Empty by default —
    # per-test fixture, no live-verified default shape needed yet.
    levels: list[dict[str, Any]] = field(default_factory=list)
    # Drawing sheets for the documentation (OST_Sheets) fetch path. Defaults to
    # the Snowdon-like fixture with a dup number + a blank name (violators).
    sheets: list[dict[str, Any]] = field(
        default_factory=lambda: copy.deepcopy(SAMPLE_REVIT_SHEETS)
    )
    # Document-identity override (wire-format của addin, camelCase). None →
    # response hardcoded cũ giữ nguyên (mọi test hiện hữu không đổi).
    document_info: dict[str, Any] | None = None
    fail_on: set[str] = field(default_factory=set)
    # Tools the (simulated) addin transport doesn't expose → raise
    # unknown_command, mirroring the HTTP-direct endpoint lacking batch.
    unsupported_commands: set[str] = field(default_factory=set)
    # Element ids that a batch STEP should report as failed ({ok: false}) rather
    # than apply — models the addin's best-effort per-step partial failure so
    # tests can assert only the succeeding fixes are marked executed (H2).
    batch_fail_eids: set[int] = field(default_factory=set)
    # Warnings revit_configure_schedule should report in an OTHERWISE-OK
    # envelope — the addin's way of saying "I skipped a field/filter". Empty
    # by default (clean configure); set per-test to exercise the honesty path.
    configure_warnings: list[str] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Pre-populate element_info with wall/door instances + their types
        # so get_element_info(typeId) works out of the box. Per-test
        # overrides still win because we only fill keys not already set.
        for eid, info in SAMPLE_FIRE_RATING_ELEMENT_INFO.items():
            self.element_info.setdefault(eid, copy.deepcopy(info))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    # --- low-level interface (matches RevitMCPClient) --------------------

    async def call_envelope(
        self, tool: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        args = arguments or {}
        self.calls.append((tool, args))
        if tool in self.unsupported_commands:
            from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError
            raise RevitEnvelopeError(
                tool=tool, code="unknown_command",
                message=f"No command registered for '{tool}'.",
            )
        if tool in self.fail_on:
            from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError
            raise RevitEnvelopeError(
                tool=tool, code="simulated_failure", message=f"Simulated: {tool}"
            )
        data = self._dispatch(tool, args)
        envelope: dict[str, Any] = {"ok": True, "data": data}
        if tool in {"revit_set_parameter", "revit_set_parameter_batch",
                    "revit_rename_element", "revit_batch",
                    "revit_create_schedule", "revit_configure_schedule",
                    "revit_apply_view_filter", "revit_color_override_by_param"}:
            dry_run = bool(args.get("dryRun", False))
            envelope["dryRun"] = dry_run
            envelope["committed"] = not dry_run
        return envelope

    async def call_data(
        self, tool: str, arguments: dict[str, Any] | None = None
    ) -> Any:
        envelope = await self.call_envelope(tool, arguments)
        return envelope.get("data")

    # --- typed convenience wrappers --------------------------------------

    async def ping(self) -> dict[str, Any]:
        return await self.call_data("revit_ping")

    async def get_document_info(self) -> dict[str, Any]:
        return await self.call_data("revit_get_document_info")

    async def get_version(self) -> dict[str, Any]:
        return await self.call_data("revit_get_version")

    async def list_levels(self) -> list[dict[str, Any]]:
        data = await self.call_data("revit_list_levels")
        return list((data or {}).get("levels", []))

    async def get_parameter(
        self, element_id: int, parameter_name: str
    ) -> dict[str, Any]:
        return await self.call_data(
            "revit_get_parameter",
            {"id": int(element_id), "parameterName": parameter_name},
        )

    async def list_rooms(self) -> list[dict[str, Any]]:
        data = await self.call_data("revit_list_rooms")
        return list((data or {}).get("rooms", []))

    async def list_sheets(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        # Mirrors the real client: revit_list_sheets takes no args and returns
        # all sheets; the limit is applied client-side as a safety cap.
        data = await self.call_data("revit_list_sheets", None)
        sheets = data if isinstance(data, list) else list((data or {}).get("sheets", []))
        if limit is not None and limit > 0:
            return sheets[:limit]
        return sheets

    async def list_categories(self) -> list[dict[str, Any]]:
        data = await self.call_data("revit_list_categories")
        return list((data or {}).get("categories", []))

    async def list_elements(
        self,
        category: str,
        *,
        limit: int | None = None,
        only_instances: bool = True,
    ) -> list[dict[str, Any]]:
        args: dict[str, Any] = {
            "category": category,
            "onlyInstances": bool(only_instances),
        }
        if limit is not None:
            args["limit"] = int(limit)
        data = await self.call_data("revit_list_elements", args)
        return list((data or {}).get("elements", []))

    async def get_element_info(self, element_id: int) -> dict[str, Any]:
        return await self.call_data("revit_get_element_info", {"id": int(element_id)})

    async def find_elements(
        self,
        category: str,
        *,
        filters: list[dict[str, Any]] | None = None,
        fields: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"category": category}
        if filters is not None:
            args["filters"] = filters
        if fields is not None:
            args["fields"] = fields
        if limit is not None:
            args["limit"] = int(limit)
        data = await self.call_data("revit_find_elements", args)
        return list((data or {}).get("elements", []))

    async def get_element_geometry(self, element_id: int) -> dict[str, Any]:
        return await self.call_data(
            "revit_get_element_geometry", {"id": int(element_id)}
        )

    async def get_linked_files(self) -> list[dict[str, Any]]:
        data = await self.call_data("revit_get_linked_files")
        return list((data or {}).get("links", []))

    async def list_spaces(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        args: dict[str, Any] = {}
        if limit is not None:
            args["limit"] = int(limit)
        data = await self.call_data("revit_list_spaces", args)
        return list((data or {}).get("spaces", []))

    async def get_active_view(self) -> dict[str, Any]:
        data = await self.call_data("revit_get_active_view")
        return data or {}

    async def get_views(self) -> list[dict[str, Any]]:
        data = await self.call_data("revit_get_views")
        return list((data or {}).get("views", []))

    async def check_clearance(
        self,
        *,
        set_a_category: str,
        set_b_category: str,
        axis: str,
        direction: str | None = None,
        clearance_mm: float,
        set_b_link_id: int | None = None,
        view_id: int | None = None,
        sample_count: int = 3,
        set_a_limit: int | None = None,
        max_results: int | None = None,
    ) -> list[dict[str, Any]]:
        set_a: dict[str, Any] = {"source": "host", "categories": [set_a_category]}
        if set_a_limit is not None:
            set_a["limit"] = int(set_a_limit)
        if set_b_link_id is not None:
            set_b: dict[str, Any] = {
                "source": "link", "linkId": set_b_link_id,
                "categories": [set_b_category],
            }
        else:
            set_b = {"source": "host", "categories": [set_b_category]}
        args: dict[str, Any] = {
            "setA": set_a,
            "setB": set_b,
            "axis": axis,
            "clearanceMm": clearance_mm,
        }
        if axis == "Z":
            if direction is not None:
                args["direction"] = direction
            args["sampleCount"] = sample_count
        if view_id is not None:
            args["viewId"] = view_id
        if max_results is not None:
            args["maxResults"] = int(max_results)
        data = await self.call_data("revit_check_clearance", args)
        return data if isinstance(data, list) else list((data or {}).get("clashes", []))

    async def set_parameter(
        self,
        element_id: int,
        parameter_name: str,
        value: Any,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return await self.call_envelope(
            "revit_set_parameter",
            {
                "id": int(element_id),
                "parameterName": parameter_name,
                "value": value,
                "dryRun": bool(dry_run),
            },
        )

    async def set_parameter_batch(
        self,
        element_ids: list[int],
        parameter_name: str,
        value: Any,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return await self.call_envelope(
            "revit_set_parameter_batch",
            {
                "ids": [int(i) for i in element_ids],
                "parameterName": parameter_name,
                "value": value,
                "dryRun": bool(dry_run),
            },
        )

    async def get_element_rooms(self, ids: list[int]) -> dict[str, Any]:
        return await self.call_data("revit_get_element_rooms", {"ids": list(ids)})

    async def select_elements(self, ids: list[int]) -> dict[str, Any]:
        return await self.call_data("revit_select_elements", {"ids": list(ids)})

    async def zoom_to_elements(self, ids: list[int]) -> dict[str, Any]:
        return await self.call_data("revit_zoom_to_elements", {"ids": list(ids)})

    # 2026-08-17 — the per-level highlight walk (`bim_orchestrator/highlight.py`).
    async def open_view(self, view_id: int, *, dry_run: bool = False) -> dict[str, Any]:
        return await self.call_data(
            "revit_open_view", {"viewId": int(view_id), "dryRun": bool(dry_run)}
        )

    async def override_element_graphics(
        self,
        *,
        view_id: int,
        element_ids: list[int],
        color: dict[str, int] | None = None,
        transparency: int | None = None,
        reset: bool = False,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "viewId": int(view_id),
            "elementIds": [int(e) for e in element_ids],
        }
        if reset:
            args["reset"] = True
        if color is not None:
            args["color"] = color
        if transparency is not None:
            args["transparency"] = int(transparency)
        return await self.call_data("revit_override_element_graphics", args)

    async def health(self) -> dict[str, Any]:
        return {"ok": True, "service": "mock-revit-addin", "version": "mock", "authEnabled": False}

    async def rename_element(
        self, element_id: int, new_name: str, *, dry_run: bool = True
    ) -> dict[str, Any]:
        return await self.call_envelope(
            "revit_rename_element",
            {"id": int(element_id), "name": new_name, "dryRun": bool(dry_run)},
        )

    async def list_families(
        self, category: str | None = None, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        args: dict[str, Any] = {}
        if category is not None:
            args["category"] = category
        if limit is not None:
            args["limit"] = int(limit)
        data = await self.call_data("revit_list_families", args or None)
        return list((data or {}).get("families", []))

    async def batch(
        self,
        steps: list[dict[str, Any]],
        *,
        dry_run: bool = False,
        stop_on_error: bool = True,
    ) -> dict[str, Any]:
        return await self.call_envelope(
            "revit_batch",
            {"steps": steps, "dryRun": bool(dry_run), "stopOnError": bool(stop_on_error)},
        )

    # --- view-authoring (v1 report module, Phase 2) ----------------------

    async def create_schedule(
        self, category: str, *, name: str | None = None,
        fields: list[str] | None = None, dry_run: bool = False,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"category": category, "dryRun": bool(dry_run)}
        if name is not None:
            args["name"] = name
        if fields is not None:
            args["fields"] = list(fields)
        return await self.call_data("revit_create_schedule", args)

    async def configure_schedule(
        self, schedule_id: int, *, filters: list[dict[str, Any]] | None = None,
        sort_fields: list[dict[str, Any]] | None = None, clear_filters: bool = False,
        clear_sort_fields: bool = False, export_csv: bool = False, dry_run: bool = False,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"scheduleId": int(schedule_id), "dryRun": bool(dry_run)}
        if filters is not None:
            args["filters"] = filters
        if sort_fields is not None:
            args["sortFields"] = sort_fields
        if clear_filters:
            args["clearFilters"] = True
        if clear_sort_fields:
            args["clearSortFields"] = True
        if export_csv:
            args["exportCsv"] = True
        return await self.call_data("revit_configure_schedule", args)

    async def apply_view_filter(
        self, *, filter_name: str, category: str, parameter_name: str, value: str,
        view_id: int | None = None, color_rgb: dict[str, int] | None = None,
        reuse_existing: bool = True, visible: bool | None = None, dry_run: bool = False,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "filterName": filter_name, "category": category,
            "parameterName": parameter_name, "value": value,
            "reuseExisting": bool(reuse_existing), "dryRun": bool(dry_run),
        }
        if view_id is not None:
            args["viewId"] = int(view_id)
        if color_rgb is not None:
            args["colorRGB"] = color_rgb
        if visible is not None:
            args["visible"] = bool(visible)
        return await self.call_data("revit_apply_view_filter", args)

    async def color_override_by_param(
        self, *, category: str, parameter_name: str,
        color_map: dict[str, dict[str, int]], view_id: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "category": category, "parameterName": parameter_name,
            "colorMap": color_map, "dryRun": bool(dry_run),
        }
        if view_id is not None:
            args["viewId"] = int(view_id)
        return await self.call_data("revit_color_override_by_param", args)

    # --- test helpers ----------------------------------------------------

    def calls_to(self, tool: str) -> list[dict[str, Any]]:
        return [args for name, args in self.calls if name == tool]

    # --- internal dispatch -----------------------------------------------

    def _dispatch(self, tool: str, args: dict[str, Any]) -> Any:
        if tool == "revit_ping":
            return {
                "pong": True,
                "hasActiveDocument": True,
                "activeDocumentTitle": "MockDocument",
            }
        if tool == "revit_get_document_info":
            if self.document_info is not None:
                return copy.deepcopy(self.document_info)
            return {
                "title": "MockDocument",
                "pathName": "C:\\mock\\Mock.rvt",
                "displayUnitSystem": "IMPERIAL",
            }
        if tool == "revit_get_version":
            return {"versionName": "Autodesk Revit 2026", "versionNumber": "2026"}
        if tool == "revit_list_levels":
            return {"count": len(self.levels), "levels": list(self.levels)}
        if tool == "revit_list_rooms":
            return {"count": len(self.rooms), "rooms": list(self.rooms)}
        if tool == "revit_list_sheets":
            # Real addin: no args, returns every sheet (count + sheets list).
            return {"count": len(self.sheets), "sheets": list(self.sheets)}
        if tool == "revit_list_categories":
            # Derive from elements_by_category — one entry per category that
            # actually has elements seeded. Mirrors the real Revit addin
            # response shape: {id, name, builtInCategory, instanceCount}
            # (see RevitMCPServer/.../ListCategoriesCommand.cs).
            cats = []
            for idx, (ost, els) in enumerate(self.elements_by_category.items()):
                if not els:
                    continue
                # Strip OST_ prefix for human-friendly name (real Revit
                # returns e.g. "Walls" via cat.Name).
                display = ost[4:] if ost.startswith("OST_") else ost
                cats.append({
                    "id": -2000000 - idx,  # synthetic BuiltInCategory id
                    "name": display,
                    "builtInCategory": ost,
                    "instanceCount": len(els),
                })
            return {"count": len(cats), "categories": cats}
        if tool == "revit_list_elements":
            category = args.get("category")
            elements = list(self.elements_by_category.get(category, []))
            limit = args.get("limit")
            if isinstance(limit, int) and limit > 0:
                truncated = len(elements) > limit
                elements = elements[:limit]
            else:
                truncated = False
            return {
                "count": len(elements),
                "limit": limit,
                "truncated": truncated,
                "elements": elements,
            }
        if tool == "revit_find_elements":
            return self._dispatch_find_elements(args)
        if tool == "revit_get_element_info":
            eid = int(args["id"])
            if eid not in self.element_info:
                from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError
                raise RevitEnvelopeError(
                    tool=tool, code="not_found", message=f"Element {eid} not found"
                )
            return dict(self.element_info[eid])
        if tool == "revit_get_parameter":
            eid = int(args["id"])
            pname = args["parameterName"]
            info = self.element_info.get(eid)
            if info is None:
                from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError
                raise RevitEnvelopeError(
                    tool=tool, code="not_found", message=f"Element {eid} not found"
                )
            param = next((p for p in info.get("parameters", []) if p["name"] == pname), None)
            if param is None:
                from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError
                raise RevitEnvelopeError(
                    tool=tool, code="not_found",
                    message=f"Parameter '{pname}' not found on element {eid}",
                )
            return dict(param)
        if tool == "revit_get_element_geometry":
            eid = int(args["id"])
            info = self.element_info.get(eid)
            if info is None:
                from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError
                raise RevitEnvelopeError(
                    tool=tool, code="not_found", message=f"Element {eid} not found"
                )
            bbox = info["boundingBox"]
            return {
                "id": eid,
                "name": info["name"],
                "solidCount": 1,
                "faceCount": 6,
                "boundingBox": bbox,
                "centroid": {
                    "x": (bbox["min"]["x"] + bbox["max"]["x"]) / 2,
                    "y": (bbox["min"]["y"] + bbox["max"]["y"]) / 2,
                    "z": (bbox["min"]["z"] + bbox["max"]["z"]) / 2,
                },
            }
        if tool == "revit_list_spaces":
            spaces = list(self.spaces)
            limit = args.get("limit")
            if isinstance(limit, int) and limit > 0:
                spaces = spaces[:limit]
            return {"count": len(spaces), "spaces": spaces}
        if tool == "revit_get_element_rooms":
            # No containment model in-memory — mirror the real shape
            # ({id, room|fromRoom/toRoom}) with room fields absent (None),
            # like an unhosted / unenclosed element would report.
            ids = args.get("ids") or []
            return {"results": [{"id": int(i), "room": None} for i in ids]}
        if tool == "revit_select_elements":
            ids = args.get("ids") or []
            return {"selected": [int(i) for i in ids]}
        if tool == "revit_zoom_to_elements":
            ids = args.get("ids") or []
            return {"zoomed": [int(i) for i in ids]}
        if tool == "revit_open_view":
            # 2026-08-17: the per-level highlight walk activates a view before
            # framing. Mirror the addin's shape AND its side effect — the real
            # one changes what `get_active_view` then reports, so a mock that
            # only echoed would be stricter-than-reality in the other direction
            # (the Q20 lesson: signature parity is not behaviour parity).
            view_id = int(args.get("viewId") or 0)
            match = next(
                (v for v in self.all_views if int(v.get("id") or 0) == view_id), None
            )
            if match is not None:
                self.active_view = dict(match)
            return {
                "viewId": view_id,
                "name": (match or {}).get("name"),
                "viewType": (match or {}).get("viewType"),
            }
        if tool == "revit_override_element_graphics":
            # Presentation-only in the real addin too (no parameter changes);
            # echo what was asked so a caller can assert colour vs reset.
            ids = args.get("elementIds") or []
            return {
                "viewId": int(args.get("viewId") or 0),
                "overridden": [int(i) for i in ids],
                "reset": bool(args.get("reset", False)),
            }
        if tool == "revit_get_active_view":
            return self.active_view or {}
        if tool == "revit_get_views":
            return {"count": len(self.all_views), "views": list(self.all_views)}
        if tool == "revit_get_linked_files":
            return {"count": len(self.linked_files), "links": list(self.linked_files)}
        if tool == "revit_check_clearance":
            # Parity with CheckClearanceCommand.cs (RunRaycastClash): the addin
            # only returns pairs CLOSER than the requested clearanceMm
            # (``if (proximityMm >= clearanceMm) continue``) and caps rows at
            # maxResults. This mock used to return the fixture list verbatim —
            # which is exactly how the clearance_max dead check (H-01) stayed
            # green: a 100 mm call happily "returned" a 150 mm pair no real
            # addin would emit. Rows without a measured distance (bbox-mode
            # fixtures) pass through: inflation can't be simulated without
            # geometry, same posture as the rest of this mock.
            limit_mm = args.get("clearanceMm")
            rows = []
            for c in self.clearance_violations:
                actual = c.get("clearanceActualMm")
                if (
                    limit_mm is not None
                    and actual is not None
                    and float(actual) >= float(limit_mm)
                ):
                    continue
                rows.append(c)
            max_rows = int(args.get("maxResults") or 0)
            if max_rows > 0:
                rows = rows[:max_rows]
            return {"clashCount": len(rows), "clashes": rows}
        if tool == "revit_set_parameter":
            return self._apply_set_parameter(args)
        if tool == "revit_set_parameter_batch":
            return self._apply_set_parameter_batch(args)
        if tool == "revit_batch":
            return self._dispatch_batch(args)
        if tool == "revit_rename_element":
            return self._apply_rename(args)
        if tool == "revit_list_families":
            fams = self.families
            cat = (args or {}).get("category")
            if cat:
                fams = [f for f in fams if cat in (f.get("category"), f.get("categoryEnum"))]
            limit = (args or {}).get("limit")
            if isinstance(limit, int) and limit > 0:
                fams = fams[:limit]
            return {"count": len(fams), "families": list(fams)}
        if tool == "revit_create_schedule":
            # Synthetic, deterministic schedule id (this call is already in
            # self.calls, so the count is >= 1 → first schedule = 900001).
            sid = 900000 + len(self.calls_to("revit_create_schedule"))
            requested_name = args.get("name")
            name = requested_name
            if requested_name and (
                any(ch in requested_name for ch in _REVIT_FORBIDDEN_VIEW_NAME_CHARS)
                or requested_name in {v.get("name") for v in self.all_views}
            ):
                # LIVE PROBE 2026-07-12 (addin v0.8.13, F1/F3): Revit silently
                # refuses a view name with forbidden characters (`[]{}|;<>?~:\`)
                # AND silently refuses a duplicate name — no error either way,
                # just a fallback to its own default. Mirror that here so a
                # verification_views test can't stay green against a naming
                # convention the real addin would quietly reject.
                name = f"Door Schedule {sid - 900000}"
            # v1.5-R6: mirror a real addin's behaviour — a committed (non-
            # dry-run) create makes the schedule show up in a later
            # revit_get_views() call, which is exactly what
            # verification_views._probe_existing_schedules relies on for
            # idempotency (--create-verification-views run twice → 2nd run
            # sees the 1st run's schedules as "existing").
            if not args.get("dryRun") and name:
                self.all_views.append({"id": sid, "name": name, "viewType": "Schedule"})
            return {
                "scheduleId": sid,
                "name": name,
                "category": args.get("category"),
                "fields": args.get("fields") or [],
            }
        if tool == "revit_configure_schedule":
            filters = args.get("filters") or []
            # Wire contract, tracking the REAL bridge by version:
            #   v0.8.21 (LIVE 2026-08-01): `filters[].value` was z.string() —
            #     a number failed MCP input validation above the bridge and
            #     surfaced client-side as an opaque `bad_envelope`.
            #   v0.8.23 (2026-08-05, RevitMCPServer handoff response): the
            #     schema is z.union([z.string(), z.number()]) and the C# side
            #     builds a typed ScheduleFilter from either form. The mock
            #     briefly stayed on the OLD contract, which meant it would
            #     fail the exact wire form the live addin now accepts —
            #     stricter-than-reality is as misleading as looser.
            # bool is rejected EXPLICITLY: in Python `bool` is a subclass of
            # `int`, so a bare isinstance((str, int, float)) check would wave
            # `True` through — and ScheduleFilter has no boolean form (the
            # bridge really does reject it).
            for f in filters:
                value = f.get("value")
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, (str, int, float))
                ):
                    from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError
                    raise RevitEnvelopeError(
                        tool=tool,
                        code="bad_envelope",
                        message=(
                            "Bridge returned a result with no parseable JSON "
                            "envelope. Raw result: MCP error -32602: Input "
                            "validation error: Invalid arguments for tool "
                            "revit_configure_schedule: filters.0.value: "
                            "expected string or number, received "
                            f"{type(value).__name__}"
                        ),
                    )
            # Real addin data keys (docs/COMMANDS.md#configure_schedule):
            # filtersAdded / sortFieldsAdded, plus `warnings` when it skipped
            # something. `configure_warnings` lets a test drive that path.
            data: dict[str, Any] = {
                "scheduleId": args.get("scheduleId"),
                "scheduleName": f"Schedule {args.get('scheduleId')}",
                "filtersAdded": list(filters),
                "sortFieldsAdded": list(args.get("sortFields") or []),
            }
            if self.configure_warnings:
                data["warnings"] = list(self.configure_warnings)
            return data
        if tool == "revit_apply_view_filter":
            return {
                "filterName": args.get("filterName"),
                "viewId": args.get("viewId"),
                "applied": True,
            }
        if tool == "revit_color_override_by_param":
            return {
                "category": args.get("category"),
                "viewId": args.get("viewId"),
                "applied": True,
                "colors": len(args.get("colorMap") or {}),
            }
        raise NotImplementedError(f"MockRevitMCPClient does not support tool: {tool}")

    def _dispatch_find_elements(self, args: dict[str, Any]) -> dict[str, Any]:
        """Mirror the Revit addin's revit_find_elements field projection.

        Projects INSTANCE-level params only (from element_info), omitting
        null/blank values (the addin leaves absent fields out of the dict),
        and attaches a ``<field>_display`` mirror from valueString. Type-level
        params are NOT resolved here — matching the live tool's behaviour and
        the reason the agent still fetches the Type via get_element_info.
        """
        category = args.get("category")
        requested = args.get("fields") or []
        limit = args.get("limit")
        base = list(self.elements_by_category.get(category, []))
        truncated = False
        if isinstance(limit, int) and limit > 0:
            truncated = len(base) > limit
            base = base[:limit]
        out: list[dict[str, Any]] = []
        for el in base:
            eid = el.get("id")
            info = self.element_info.get(int(eid)) if eid is not None else None
            proj: dict[str, Any] = {}
            if info:
                pmap = {p["name"]: p for p in info.get("parameters", [])}
                for fname in requested:
                    p = pmap.get(fname)
                    if p is None:
                        continue
                    val = p.get("value")
                    if val is None:
                        continue
                    if isinstance(val, str) and not val.strip():
                        continue
                    proj[fname] = val
                    vs = p.get("valueString")
                    if vs is not None:
                        proj[f"{fname}_display"] = vs
            out.append({
                "id": eid,
                "name": el.get("name"),
                "category": el.get("category"),
                "categoryEnum": el.get("categoryEnum"),
                "typeId": el.get("typeId"),
                "fields": proj,
            })
        return {
            "count": len(out),
            "limit": limit,
            "truncated": truncated,
            "elements": out,
        }

    def _dispatch_batch(self, args: dict[str, Any]) -> dict[str, Any]:
        """Apply each step inside one logical transaction (mock = sequential).

        Honours the batch-level dryRun. stopOnError rolls back conceptually;
        the mock applies best-effort and reports per-step results.
        """
        steps = args.get("steps", [])
        dry_run = bool(args.get("dryRun", False))
        results: list[dict[str, Any]] = []
        for step in steps:
            cmd = str(step.get("command", "")).removeprefix("revit_")
            p = dict(step.get("params", {}))
            p["dryRun"] = dry_run
            # H2: forced per-step failure — report {ok: false} for this step
            # instead of applying, so the batch partially succeeds.
            try:
                _sid = int(p.get("id"))
            except (TypeError, ValueError):
                _sid = None
            if _sid is not None and _sid in self.batch_fail_eids:
                results.append({"ok": False, "error": "mock forced step failure", "id": _sid})
                continue
            if cmd == "set_parameter":
                results.append(self._apply_set_parameter(p))
            elif cmd == "rename_element":
                results.append(self._apply_rename(p))
            else:
                raise NotImplementedError(f"MockRevitMCPClient batch step: {cmd}")
        return {"count": len(results), "results": results}

    def _apply_set_parameter(self, args: dict[str, Any]) -> dict[str, Any]:
        eid = int(args["id"])
        pname = args["parameterName"]
        new_value = args.get("value")
        dry_run = bool(args.get("dryRun", False))
        info = self.element_info.get(eid)
        if info is None:
            from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError
            raise RevitEnvelopeError(
                tool="revit_set_parameter",
                code="not_found",
                message=f"Element {eid} not found",
            )
        target = next((p for p in info["parameters"] if p["name"] == pname), None)
        before = target["value"] if target else None
        if not dry_run:
            if target is None:
                info["parameters"].append(
                    {"name": pname, "value": new_value, "valueString": str(new_value)}
                )
            else:
                target["value"] = new_value
                target["valueString"] = str(new_value)
        return {
            "id": eid,
            "parameterName": pname,
            "changeSummary": f"Set '{pname}' on element {eid}: '{before}' → '{new_value}'",
            "changes": {"before": before, "after": new_value},
        }

    def _apply_set_parameter_batch(self, args: dict[str, Any]) -> dict[str, Any]:
        ids = [int(i) for i in args["ids"]]
        pname = args["parameterName"]
        value = args.get("value")
        dry_run = bool(args.get("dryRun", False))
        applied = 0
        for eid in ids:
            try:
                self._apply_set_parameter(
                    {
                        "id": eid,
                        "parameterName": pname,
                        "value": value,
                        "dryRun": dry_run,
                    }
                )
                applied += 1
            except Exception:
                pass
        return {
            "count": applied,
            "parameterName": pname,
            "changeSummary": f"Set '{pname}' on {applied} elements → '{value}'",
        }

    def _apply_rename(self, args: dict[str, Any]) -> dict[str, Any]:
        eid = int(args["id"])
        new_name = args["name"]
        dry_run = bool(args.get("dryRun", False))
        info = self.element_info.get(eid)
        if info is None:
            from bim_orchestrator.mcp_clients.revit import RevitEnvelopeError
            raise RevitEnvelopeError(
                tool="revit_rename_element",
                code="not_found",
                message=f"Element {eid} not found",
            )
        before = info["name"]
        if not dry_run:
            info["name"] = new_name
            name_param = next(
                (p for p in info["parameters"] if p["name"] == "Name"), None
            )
            if name_param is not None:
                name_param["value"] = new_name
                name_param["valueString"] = new_name
        return {
            "id": eid,
            "changeSummary": f"Renamed element {eid}: '{before}' → '{new_name}'",
            "changes": {"before": before, "after": new_name},
        }


# ---------------------------------------------------------------------------
# P3-1 — audit satellite fakes (lod-validator / spatial-qc stdio clients).
# 1:1 method surface with the real clients (guarded by test_mock_parity.py);
# in-memory envelopes, no subprocess. `fail_on_enter` simulates a satellite
# whose venv/server can't spawn (AuditAxisError path).
# ---------------------------------------------------------------------------


@dataclass
class MockLODValidatorClient:
    """Test double for ``mcp_clients.lod_validator.LODValidatorClient``."""

    envelope: dict[str, Any] = field(default_factory=lambda: {
        "schema": "lod-validator/phase0",
        "required_lod": 300,
        "summary": {
            "total": 0, "passed": 0, "failed": 0, "undecided": 0,
            "detected_lod_distribution": {},
        },
        "results": [],
    })
    fail_on_enter: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __aenter__(self) -> Self:
        if self.fail_on_enter:
            from bim_orchestrator.mcp_clients.lod_validator import AuditAxisError
            raise AuditAxisError("lod", "failed to start lod_validator.server (mock)")
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def validate_lod(
        self,
        ifc_path: str,
        required_lod: int,
        classes: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({
            "tool": "validate_lod", "ifc_path": ifc_path,
            "required_lod": required_lod, "classes": classes,
        })
        return copy.deepcopy(self.envelope)

    async def emit_bcf(
        self,
        ifc_path: str,
        required_lod: int,
        out_path: str,
        only_failures: bool = True,
    ) -> dict[str, Any]:
        self.calls.append({
            "tool": "emit_bcf", "ifc_path": ifc_path,
            "required_lod": required_lod, "out_path": out_path,
            "only_failures": only_failures,
        })
        from pathlib import Path
        failures = sum(
            1 for r in self.envelope.get("results", []) if r.get("passed") is False
        )
        Path(out_path).write_bytes(b"PK\x03\x04 mock bcfzip")
        return {"bcf_path": out_path, "topics": failures, "failures": failures}


@dataclass
class MockSpatialQCClient:
    """Test double for ``mcp_clients.spatial_qc.SpatialQCClient``."""

    envelope: dict[str, Any] = field(default_factory=lambda: {
        "summary": {"total": 0, "pass": 0, "fail": 0},
        "verdicts": [],
    })
    fail_on_enter: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __aenter__(self) -> Self:
        if self.fail_on_enter:
            from bim_orchestrator.mcp_clients.spatial_qc import AuditAxisError
            raise AuditAxisError("spatial", "failed to start spatial_qc.server (mock)")
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def check_building(
        self,
        ifc_path: str,
        required_width_m: float | None = None,
        rules: str | None = None,
        subtract_furniture: bool = False,
        doors_egress_only: bool = False,
    ) -> dict[str, Any]:
        self.calls.append({
            "tool": "check_building", "ifc_path": ifc_path,
            "required_width_m": required_width_m, "rules": rules,
            "subtract_furniture": subtract_furniture,
            "doors_egress_only": doors_egress_only,
        })
        return copy.deepcopy(self.envelope)

    async def emit_bcf(
        self,
        ifc_path: str,
        out_path: str,
        required_width_m: float = 1.10,
    ) -> dict[str, Any]:
        self.calls.append({
            "tool": "emit_bcf", "ifc_path": ifc_path,
            "out_path": out_path, "required_width_m": required_width_m,
        })
        from pathlib import Path
        fails = sum(
            1 for v in self.envelope.get("verdicts", [])
            if v.get("status") == "FAIL"
        )
        Path(out_path).write_bytes(b"PK\x03\x04 mock bcfzip")
        return {"bcf": out_path, "topics": fails, "fails": fails}
