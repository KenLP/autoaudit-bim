"""``Demo Villa (simulated)`` — a small, deliberately-flawed mock model.

Builds a Revit-shaped + Forma-shaped dataset (~20 elements) that exercises
every corner of the compliance loop against ``config/rules.demo.yaml``:

  * Doors / 4 Types, hosted in one Fire-Rated wall:
      - Type "36x84 (Blank FR)"  — Fire Rating blank  -> inherit host "2 HR"
      - Type "36x84 (120 MIN)"   — Fire Rating "120 min" -> normalize "2 HR"
      - Type "36x84 (2 HR OK)"   — already compliant (PASS set)
      - Type "36x84 Narrow"     — Width 610mm < 900mm minimum -> Path A
    Plus one door (705) on the compliant type with a badly-formatted Mark
    ("D 105") -> auto-normalizes to "D_105" (v1.4-K16).
  * Rooms:
      - 1 missing Department -> auto-fills "General"
      - 2 sharing Number "101" -> approve-gated renumber proposal (101A/101B)
      - 5 fully compliant (PASS set)

All ids are namespaced away from the ``tests/_mocks.py`` sample fixtures
(``SAMPLE_REVIT_*``) so nothing collides when both are loaded in the same
process (``MockRevitMCPClient.__post_init__`` merges in the legacy fixture's
ids too — harmless since this dataset's ids never reference them).
"""

from __future__ import annotations

import copy
from typing import Any

from bim_orchestrator.demo.clients import _import_mocks
from bim_orchestrator.policies.demo_identity import (
    DEMO_PROJECT_ID as _DEMO_PROJECT_ID,
)

# Stable label for the run folder / report — every artifact from --demo
# carries this so it's obvious the run is simulated, not a real project.
DEMO_PROJECT_ID = _DEMO_PROJECT_ID  # single source: policies/demo_identity.py

# ---------------------------------------------------------------------------
# Host wall — one "2 HR" rated corridor wall hosting every demo door.
# ---------------------------------------------------------------------------

_WALL_INSTANCE_ID = 9000
_WALL_TYPE_ID = 9050

_WALL_ELEMENT_INFO: dict[int, dict[str, Any]] = {
    _WALL_INSTANCE_ID: {
        "id": _WALL_INSTANCE_ID,
        "name": "Corridor Wall 2HR",
        "category": "Walls",
        "categoryEnum": "OST_Walls",
        "typeId": _WALL_TYPE_ID,
        "parameters": [
            {"name": "Type Id", "value": _WALL_TYPE_ID, "valueString": str(_WALL_TYPE_ID)},
            {"name": "Mark", "value": None, "valueString": None},
        ],
    },
    _WALL_TYPE_ID: {
        "id": _WALL_TYPE_ID,
        "name": "Corridor - 2HR Rated",
        "category": "Walls",
        "categoryEnum": "OST_Walls",
        "typeId": -1,
        "parameters": [
            {"name": "Family Name", "value": "Basic Wall"},
            {"name": "Type Name", "value": "Corridor - 2HR Rated"},
            {"name": "Fire Rating", "value": "2 HR", "valueString": "2 HR"},
        ],
    },
}

# ---------------------------------------------------------------------------
# Door types — 4 distinct types keyed by typeId. Width in feet (Revit
# internal storage unit); valueString carries the display mm mirror.
# ---------------------------------------------------------------------------

_DOOR_TYPE_BLANK_FR = 9101   # Fire Rating "" — inherit from host
_DOOR_TYPE_BAD_FMT = 9102    # Fire Rating "120 min" — normalize
_DOOR_TYPE_OK = 9103         # Fire Rating "2 HR" — compliant (PASS set)
_DOOR_TYPE_NARROW = 9104     # Fire Rating "2 HR" OK, but Width < 900mm

_COMPLIANT_WIDTH_FT = 3.0   # 914mm — clears the 900mm minimum
_NARROW_WIDTH_FT = 2.0      # 610mm — below the 900mm minimum


def _door_type(
    type_id: int, *, type_name: str, fire_rating: str, width_ft: float
) -> dict[str, Any]:
    return {
        "id": type_id,
        "name": type_name,
        "category": "Doors",
        "categoryEnum": "OST_Doors",
        "typeId": -1,
        "parameters": [
            {"name": "Family Name", "value": "Door-Single-Flush"},
            {"name": "Type Name", "value": type_name},
            {"name": "Type Mark", "value": type_name[:6]},
            {"name": "Fire Rating", "value": fire_rating, "valueString": fire_rating},
            {"name": "Width", "value": width_ft, "valueString": str(round(width_ft * 304.8))},
        ],
    }


_DOOR_TYPES_ELEMENT_INFO: dict[int, dict[str, Any]] = {
    _DOOR_TYPE_BLANK_FR: _door_type(
        _DOOR_TYPE_BLANK_FR, type_name="36x84 (Blank FR)",
        fire_rating="", width_ft=_COMPLIANT_WIDTH_FT,
    ),
    _DOOR_TYPE_BAD_FMT: _door_type(
        _DOOR_TYPE_BAD_FMT, type_name="36x84 (120 MIN)",
        fire_rating="120 min", width_ft=_COMPLIANT_WIDTH_FT,
    ),
    _DOOR_TYPE_OK: _door_type(
        _DOOR_TYPE_OK, type_name="36x84 (2 HR OK)",
        fire_rating="2 HR", width_ft=_COMPLIANT_WIDTH_FT,
    ),
    _DOOR_TYPE_NARROW: _door_type(
        _DOOR_TYPE_NARROW, type_name="36x84 Narrow",
        fire_rating="2 HR", width_ft=_NARROW_WIDTH_FT,
    ),
}

# ---------------------------------------------------------------------------
# Door instances — (element_id, type_id, mark). 705 carries the deliberately
# malformed Mark ("D 105" instead of "D_105"); every other Mark is already
# canonical so it doesn't accidentally also fire the naming rule.
# ---------------------------------------------------------------------------

_DOOR_INSTANCES: list[tuple[int, int, str]] = [
    (701, _DOOR_TYPE_BLANK_FR, "D_101"),
    (702, _DOOR_TYPE_BLANK_FR, "D_102"),
    (703, _DOOR_TYPE_BAD_FMT, "D_103"),
    (704, _DOOR_TYPE_BAD_FMT, "D_104"),
    (705, _DOOR_TYPE_OK, "D 105"),   # <- malformed Mark (K16 auto-normalize)
    (706, _DOOR_TYPE_OK, "D_106"),
    (707, _DOOR_TYPE_NARROW, "D_107"),  # <- narrow width (Path A)
    (708, _DOOR_TYPE_OK, "D_108"),
    (709, _DOOR_TYPE_OK, "D_109"),
    (710, _DOOR_TYPE_OK, "D_110"),
    (711, _DOOR_TYPE_OK, "D_111"),
    (712, _DOOR_TYPE_OK, "D_112"),
]

_DOOR_TYPE_NAMES = {tid: info["name"] for tid, info in _DOOR_TYPES_ELEMENT_INFO.items()}


def _door_instance_info(element_id: int, type_id: int, mark: str) -> dict[str, Any]:
    return {
        "id": element_id,
        "name": _DOOR_TYPE_NAMES[type_id],
        "category": "Doors",
        "categoryEnum": "OST_Doors",
        "typeId": type_id,
        "parameters": [
            {"name": "Type Id", "value": type_id, "valueString": str(type_id)},
            {"name": "Host Id", "value": _WALL_INSTANCE_ID, "valueString": str(_WALL_INSTANCE_ID)},
            {"name": "Mark", "value": mark, "valueString": mark},
            {"name": "Comments", "value": None, "valueString": None},
        ],
    }


_DOOR_INSTANCES_ELEMENT_INFO: dict[int, dict[str, Any]] = {
    eid: _door_instance_info(eid, type_id, mark) for eid, type_id, mark in _DOOR_INSTANCES
}

_DOORS_LISTING: list[dict[str, Any]] = [
    {
        "id": eid, "name": _DOOR_TYPE_NAMES[type_id], "category": "Doors",
        "categoryEnum": "OST_Doors", "typeId": type_id,
    }
    for eid, type_id, _mark in _DOOR_INSTANCES
]

# ---------------------------------------------------------------------------
# Rooms — (element_id, name, number, department). 401 is missing Department;
# 402/403 share Number "101"; the rest are fully compliant (PASS set).
# ---------------------------------------------------------------------------

_ROOMS: list[tuple[int, str, str, str | None]] = [
    (401, "Guest Bedroom", "201", None),               # missing Department
    (402, "Living Room", "101", "Residential"),         # duplicate Number #1
    (403, "Dining Room", "101", "Residential"),         # duplicate Number #2
    (404, "Kitchen", "102", "Residential"),             # compliant
    (405, "Storage Closet", "103", "Services"),         # compliant
    (406, "Corridor", "104", "Circulation"),            # compliant
    (407, "Guest Bedroom 2", "105", "Residential"),     # compliant
    (408, "Powder Room", "106", "Wet"),                 # compliant
]


def _room_instance_info(
    element_id: int, name: str, number: str, department: str | None
) -> dict[str, Any]:
    return {
        "id": element_id,
        "name": name,
        "category": "Rooms",
        "levelId": 1,
        "parameters": [
            {"name": "Name", "value": name, "valueString": name},
            {"name": "Number", "value": number, "valueString": number},
            {"name": "Department", "value": department, "valueString": department},
            {"name": "Area", "value": 120.0, "valueString": "120 SF"},
        ],
    }


_ROOMS_ELEMENT_INFO: dict[int, dict[str, Any]] = {
    eid: _room_instance_info(eid, name, number, dept) for eid, name, number, dept in _ROOMS
}

_ROOMS_LISTING: list[dict[str, Any]] = [
    {
        "id": eid, "name": name, "category": "Rooms", "categoryEnum": "OST_Rooms",
        "typeId": -1, "levelName": "L1", "areaMetric": 11.15, "perimeter": 14.0,
    }
    for eid, name, _number, _dept in _ROOMS
]


def build_demo_clients() -> tuple[Any, Any]:
    """Construct the (Revit, Forma) mock clients for the Demo Villa dataset.

    Returns the SAME classes ``tests/_mocks.py`` uses everywhere else
    (``MockRevitMCPClient``, ``MockFormaMCPClient``) — see ``demo/clients.py``
    for why. Both are async context managers (``async with client: ...``),
    so they slot directly into ``orchestrator.run_revit``'s existing
    ``revit_client_factory`` / ``forma_client_factory`` injection points —
    zero network, zero API key, zero real Revit/ACC.
    """
    MockRevitMCPClient, MockFormaMCPClient = _import_mocks()

    # Deep-copy every fixture dict: MockRevitMCPClient.set_parameter mutates
    # element_info entries IN PLACE (that's the point — it's how the demo
    # proves a real fix happened). Without a deep copy here, two calls to
    # build_demo_clients() (e.g. two tests, or two --demo runs in the same
    # process) would share and cross-contaminate the SAME module-level dicts.
    element_info: dict[int, dict[str, Any]] = {}
    element_info.update(copy.deepcopy(_WALL_ELEMENT_INFO))
    element_info.update(copy.deepcopy(_DOOR_TYPES_ELEMENT_INFO))
    element_info.update(copy.deepcopy(_DOOR_INSTANCES_ELEMENT_INFO))
    element_info.update(copy.deepcopy(_ROOMS_ELEMENT_INFO))

    revit_client = MockRevitMCPClient(
        element_info=element_info,
        elements_by_category={
            "OST_Doors": copy.deepcopy(_DOORS_LISTING),
            "OST_Rooms": copy.deepcopy(_ROOMS_LISTING),
        },
        document_info={
            # Wire-format của addin (camelCase) — _fetch_document_identity
            # chuẩn hoá về snake_case. Không có pathName: model mô phỏng,
            # không tồn tại trên disk (đừng bịa một đường dẫn giả).
            "title": "Demo Villa",
            "isWorkshared": False,
            "isModified": False,
            "projectName": "Demo Villa",
            "projectNumber": "DEMO-001",
            "displayUnitSystem": "METRIC",
        },
    )
    # Default subtypes/issues store (tests/_mocks.SAMPLE_SUBTYPES) already
    # carries an active subtype, so DesignAgent's ACC issue subtype
    # auto-discovery just works — no explicit --issue-subtype-id needed.
    forma_client = MockFormaMCPClient()
    return revit_client, forma_client


__all__ = ["DEMO_PROJECT_ID", "build_demo_clients"]
