"""Synthetic BEP §1 (Room and Space Requirements) for Phase 2 Week 6.

Hand-crafted fixture in the style of a real Building Execution Plan room
schedule appendix. The 6 entries below mirror the requirements list the
user supplied in the Week 6 kickoff (bedroom area ≥10 m², other rooms
≥9 m², bedroom width ≥2.4 m, clear height ≥2.6 m, unique name+number,
boundaries enclosed) plus a 7th classification clause supporting the
``room.department.required`` rule that Phase 1 carried over.

Source label is "BEP.txt" to make it obvious this is synthetic. Swap with
the real BEP PDF later via ``VectorStore.ingest_pdf`` — schema and chunk
shape are identical.
"""

from __future__ import annotations

from typing import Any

from bim_orchestrator.rag.eval import EvalQuery
from bim_orchestrator.rag.store import VectorStore

# Each entry becomes one chunk. Sections follow a typical BEP appendix
# numbering: §1 Room and Space Requirements, §1.x for individual clauses.
BEP_ROOM_REQUIREMENTS: list[dict[str, Any]] = [
    {
        "section": "§1.1",
        "page": 1,
        "text": (
            "Section 1.1 Minimum bedroom floor area. The minimum floor area "
            "of a bedroom shall be 10 m². This requirement applies to every "
            "room with a residential occupancy classification used as a "
            "primary sleeping space. Floor area is measured net, inside the "
            "finished wall surfaces, and excludes built-in wardrobes and "
            "closets deeper than 600 mm."
        ),
    },
    {
        "section": "§1.2",
        "page": 1,
        "text": (
            "Section 1.2 Minimum floor area for other occupied rooms. The "
            "minimum floor area of any occupied room other than a bedroom "
            "shall be 9 m². Mechanical, electrical, and circulation spaces "
            "that are not occupied by people for extended periods are exempt "
            "from this clause but remain subject to building services "
            "clearance requirements."
        ),
    },
    {
        "section": "§1.3",
        "page": 2,
        "text": (
            "Section 1.3 Minimum bedroom width. The minimum clear width of a "
            "bedroom shall be 2.4 m, measured between the inside surfaces of "
            "opposite walls. Where the room is non-rectangular, the "
            "narrowest internal dimension shall be used. This dimension "
            "ensures usable furniture layouts and accessible circulation."
        ),
    },
    {
        "section": "§1.4",
        "page": 2,
        "text": (
            "Section 1.4 Minimum clear ceiling height. The minimum clear "
            "height of an occupied room shall be 2.6 m, measured from the "
            "finished floor level to the underside of the ceiling or any "
            "overhead obstruction. Soffits, beams, and bulkheads below this "
            "height shall not encroach on more than 10 percent of the floor "
            "plan area."
        ),
    },
    {
        "section": "§1.5",
        "page": 3,
        "text": (
            "Section 1.5 Room identification. All rooms shall be provided "
            "with a unique room name and a unique room number. Numbers are "
            "assigned per level and shall not be reused across levels except "
            "where a deliberate vertical-alignment convention is documented "
            "in the BEP appendix. Duplicate numbers shall be flagged for "
            "renaming prior to construction documentation issue."
        ),
    },
    {
        "section": "§1.6",
        "page": 3,
        "text": (
            "Section 1.6 Room boundaries. Room boundaries shall be fully "
            "enclosed and shall not overlap with adjacent rooms or spaces. "
            "Room-bounding elements include walls, floors, ceilings, and "
            "room separator lines. Open-plan zones split into multiple rooms "
            "shall use approved separator lines and shall not result in "
            "double-counted floor area."
        ),
    },
    {
        "section": "§1.7",
        "page": 4,
        "text": (
            "Section 1.7 Room classification metadata. Every room shall "
            "carry a non-empty Department value indicating the functional "
            "group it serves (e.g. Residential, Commercial, Circulation, "
            "Services). Occupancy classification shall be set for every "
            "habitable space. Missing department or occupancy values shall "
            "be flagged during model review and corrected before "
            "construction documentation issue."
        ),
    },
]


def ingest_bep_room_requirements(
    store: VectorStore, *, source: str = "BEP.txt"
) -> int:
    """Ingest the synthetic BEP §1 fixture into the given store.

    Returns total number of chunks added. Each section is short enough that
    the paragraph chunker will not split it further.
    """
    total = 0
    for entry in BEP_ROOM_REQUIREMENTS:
        total += store.ingest_text(
            entry["text"],
            source=source,
            section=entry["section"],
            page=entry["page"],
        )
    return total


# Eval queries — each pairs a realistic BIM-Manager phrasing with the BEP
# section that should land at rank 1. Tune the corpus until hit@1 ≥ 60%.
DEFAULT_BEP_QUERIES: list[EvalQuery] = [
    EvalQuery(
        query="minimum bedroom floor area requirement",
        expected_source="BEP.txt",
        expected_section="§1.1",
        description="Residential unit area compliance",
    ),
    EvalQuery(
        query="minimum floor area for non-bedroom occupied rooms",
        expected_source="BEP.txt",
        expected_section="§1.2",
        description="General room area minimum (9 m²)",
    ),
    EvalQuery(
        query="minimum bedroom width 2.4 metres",
        expected_source="BEP.txt",
        expected_section="§1.3",
        description="Bedroom clear width requirement",
    ),
    EvalQuery(
        query="minimum ceiling clear height for occupied rooms",
        expected_source="BEP.txt",
        expected_section="§1.4",
        description="Clear height ≥ 2.6 m rule",
    ),
    EvalQuery(
        query="unique room number naming requirement",
        expected_source="BEP.txt",
        expected_section="§1.5",
        description="Duplicate room number flagging",
    ),
    EvalQuery(
        query="room boundaries enclosed no overlap",
        expected_source="BEP.txt",
        expected_section="§1.6",
        description="Boundary enclosure (Phase 3 rule placeholder)",
    ),
    EvalQuery(
        query="department parameter required for rooms",
        expected_source="BEP.txt",
        expected_section="§1.7",
        description="Department/Occupancy metadata required",
    ),
]
