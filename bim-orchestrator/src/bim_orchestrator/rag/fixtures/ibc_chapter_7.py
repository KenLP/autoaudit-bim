"""Synthetic IBC Chapter 7 (Fire & Smoke Protection) for RAG eval.

This is NOT the real IBC — it's a hand-crafted fixture in the *style* of IBC
sections, with realistic vocabulary so retrieval quality eval is meaningful.
Use a real IBC PDF in production; this lets us run the eval harness in CI
without copyright concerns or external file dependencies.

Source label is "IBC.txt" to make it obvious this is synthetic. Pages are
1-indexed and follow a plausible chapter layout.
"""

from __future__ import annotations

from typing import Any

from bim_orchestrator.rag.eval import EvalQuery
from bim_orchestrator.rag.store import VectorStore

# Each entry: (section, page, text). Sections follow IBC numbering conventions.
IBC_CHAPTER_7: list[dict[str, Any]] = [
    {
        "section": "§711.1",
        "page": 1,
        "text": (
            "Section 711 General. Fire walls, fire barriers, fire partitions, "
            "smoke barriers, and smoke partitions shall comply with this section. "
            "The fire-resistance ratings of building elements and components shall "
            "comply with Section 703 and Table 601."
        ),
    },
    {
        "section": "§711.2",
        "page": 2,
        "text": (
            "Section 711.2 Fire Walls. Fire walls shall be constructed of materials "
            "permitted by the building type of construction and shall be continuous "
            "from the foundation to or through the roof. Corridor walls forming an "
            "exit access in occupied buildings shall have a minimum fire-resistance "
            "rating of 2 hours where the corridor serves an occupant load greater "
            "than 30."
        ),
    },
    {
        "section": "§711.3",
        "page": 3,
        "text": (
            "Section 711.3 Fire-Resistance-Rated Assemblies. Fire-resistance-rated "
            "assemblies shall be of materials and construction tested in accordance "
            "with ASTM E119 or UL 263. Typical ratings include 1-hour, 2-hour, "
            "3-hour, and 4-hour assemblies. The required rating depends on the "
            "occupancy classification and the wall's position in the egress system."
        ),
    },
    {
        "section": "§711.4",
        "page": 4,
        "text": (
            "Section 711.4 Materials. Fire walls of structures other than Type IV "
            "construction shall be of noncombustible materials. Materials used in "
            "the construction of corridor walls and partitions shall be approved "
            "for the required fire-resistance rating."
        ),
    },
    {
        "section": "§712.1",
        "page": 6,
        "text": (
            "Section 712 Smoke Barriers. Smoke barriers shall be constructed to "
            "form an effective membrane continuous from outside wall to outside "
            "wall and from floor slab to floor or roof deck above, including "
            "continuity through concealed spaces. Smoke barriers shall have a "
            "minimum 1-hour fire-resistance rating and separate areas of different "
            "occupancy classifications."
        ),
    },
    {
        "section": "§712.2",
        "page": 7,
        "text": (
            "Section 712.2 Smoke Barrier Materials. Smoke barriers shall be "
            "constructed of materials approved for the required fire-resistance "
            "rating. Joints and voids at intersections of smoke barriers with "
            "exterior walls, floors, and roofs shall be sealed with an approved "
            "material to resist the passage of smoke."
        ),
    },
    {
        "section": "§714.1",
        "page": 10,
        "text": (
            "Section 714 Penetrations. Through-penetrations of fire-resistance-"
            "rated walls shall be protected by an approved penetration firestop "
            "system. Penetrations of fire walls, fire barriers, and smoke barriers "
            "by pipes, conduits, cables, and similar items shall be protected to "
            "maintain the rating of the assembly. Listed firestop systems shall "
            "be tested in accordance with ASTM E814 or UL 1479."
        ),
    },
    {
        "section": "§714.2",
        "page": 11,
        "text": (
            "Section 714.2 Membrane Penetrations. Membrane penetrations of fire-"
            "resistance-rated walls, floors, and ceilings by steel electrical "
            "boxes shall be permitted provided the aggregate area of openings does "
            "not exceed 100 square inches per 100 square feet of wall area."
        ),
    },
    {
        "section": "§716.1",
        "page": 14,
        "text": (
            "Section 716 Opening Protectives. Opening protectives required by other "
            "sections of this code shall be installed in accordance with this "
            "section. Fire door assemblies, fire window assemblies, and fire "
            "shutters shall comply with NFPA 80. Fire-rated doors in corridor "
            "walls shall have a minimum fire-protection rating of 20 minutes for "
            "1-hour walls and 60 minutes for 2-hour walls."
        ),
    },
    {
        "section": "§716.2",
        "page": 15,
        "text": (
            "Section 716.2 Glazing. Fire-protection-rated glazing in fire windows "
            "shall comply with NFPA 80 and shall be permanently labeled. Wired "
            "glass and ceramic glazing systems are permitted in opening "
            "protectives where the required rating does not exceed 45 minutes."
        ),
    },
]


def ingest_ibc_chapter_7(store: VectorStore, *, source: str = "IBC.txt") -> int:
    """Ingest the synthetic IBC §7 fixture into the given store.

    Returns total number of chunks added. Each section becomes one chunk
    (paragraphs short enough that the chunker won't split them further).
    """
    total = 0
    for entry in IBC_CHAPTER_7:
        total += store.ingest_text(
            entry["text"],
            source=source,
            section=entry["section"],
            page=entry["page"],
        )
    return total


# Hand-built eval queries. Each pairs a realistic BIM Manager phrasing with
# the section that *should* show up at rank 1. Edit as you tune the corpus.
DEFAULT_IBC_QUERIES: list[EvalQuery] = [
    EvalQuery(
        query="fire rating for corridor walls",
        expected_source="IBC.txt",
        expected_section="§711.2",
        description="Common BIM-Manager phrasing for corridor wall ratings",
    ),
    EvalQuery(
        query="smoke barrier between different occupancies",
        expected_source="IBC.txt",
        expected_section="§712.1",
        description="Two adjacent areas with different occupancy classifications",
    ),
    EvalQuery(
        query="2-hour fire-resistance-rated assembly requirements",
        expected_source="IBC.txt",
        expected_section="§711.3",
        description="Looking up assembly rating standards",
    ),
    EvalQuery(
        query="penetration firestop systems for pipes through fire walls",
        expected_source="IBC.txt",
        expected_section="§714.1",
        description="MEP coordination — pipes piercing rated walls",
    ),
    EvalQuery(
        query="fire door rating for corridor opening",
        expected_source="IBC.txt",
        expected_section="§716.1",
        description="Door schedule — which doors need fire labels",
    ),
    EvalQuery(
        query="membrane penetration electrical box area limit",
        expected_source="IBC.txt",
        expected_section="§714.2",
        description="Electrical-trade compliance question",
    ),
    EvalQuery(
        query="fire-rated glazing wired glass requirements",
        expected_source="IBC.txt",
        expected_section="§716.2",
        description="Window schedule rating",
    ),
]
