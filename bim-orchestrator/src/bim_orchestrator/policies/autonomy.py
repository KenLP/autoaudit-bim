"""Autonomy policy — resolves a proposed mutation to one of {auto, approve, human-only}.

Reads `config/autonomy.yaml` and maps (mutation_category, action, severity)
to a routing decision. Used by the Design Agent before every mutation call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

Decision = Literal["auto", "approve", "human-only"]


@dataclass(frozen=True)
class AutonomyPolicy:
    raw: dict

    @classmethod
    def load(cls, path: str | Path) -> "AutonomyPolicy":
        # encoding="utf-8": don't fall back to the Windows cp1252 locale, which
        # would mojibake any non-ASCII config content at load time.
        with open(path, encoding="utf-8") as f:
            return cls(raw=yaml.safe_load(f))

    def resolve_severity(self, severity_tag: str) -> str:
        return self.raw.get("severity_rules", {}).get(severity_tag, "severity_low")

    def resolve(self, category: str, action: str, severity: str) -> Decision:
        try:
            node = self.raw["mutations"][category][action]
        except KeyError:
            return "approve"  # safe default: unknown combos require human
        # Two valid shapes in autonomy.yaml:
        #   action: auto                       (decision applies at any severity)
        #   action: {severity_low: auto, ...}  (per-severity routing)
        if isinstance(node, str):
            return node  # type: ignore[return-value]
        try:
            return node[severity]  # type: ignore[no-any-return]
        except (KeyError, TypeError):
            return "approve"
