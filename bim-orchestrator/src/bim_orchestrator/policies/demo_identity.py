"""The simulated-project marker, in one place.

``--demo`` records look exactly like real approval records, which is the
point — the demo exercises the real trust pipeline. That makes the project
id the only thing distinguishing them, and two subsystems have to agree on
it: ``ApprovalWatcher`` must never apply a simulated record against real
ACC/Revit, and ``DesignAgent`` must never ask a mock Forma client about an
issue from a previous process. `policies/` is the layer both may import.
"""

from __future__ import annotations

DEMO_PROJECT_ID = "demo-villa-simulated"
