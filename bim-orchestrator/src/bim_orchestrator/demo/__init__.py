"""``bim-orchestrator --demo`` — the full compliance loop on a mock model.

Zero Revit, zero ACC, zero API key: ``build_demo_clients()`` returns the
SAME mock MCP clients the test suite pins 1:1 against the real protocol
(``tests/_mocks.py``), pre-loaded with the "Demo Villa (simulated)" dataset.
"Reasoning is live, data is staged" — see ``dataset.py`` for the dataset and
``config/rules.demo.yaml`` for the rules.
"""

from __future__ import annotations

from bim_orchestrator.demo.dataset import DEMO_PROJECT_ID, build_demo_clients

__all__ = ["DEMO_PROJECT_ID", "build_demo_clients"]
