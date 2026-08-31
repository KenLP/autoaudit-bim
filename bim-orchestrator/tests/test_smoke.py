"""Smoke tests — verify module wiring without external services."""

from __future__ import annotations


def test_package_imports() -> None:
    from bim_orchestrator import __version__
    from bim_orchestrator.mcp_clients.forma import FormaMCPClient, FormaMCPConfig
    from bim_orchestrator.policies.autonomy import AutonomyPolicy
    from bim_orchestrator.state import OrchestratorState

    assert __version__ == "0.1.0"
    assert FormaMCPClient is not None
    assert FormaMCPConfig is not None
    assert AutonomyPolicy is not None
    assert OrchestratorState is not None


def test_autonomy_policy_loads(tmp_path):
    import yaml

    from bim_orchestrator.policies.autonomy import AutonomyPolicy

    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "mutations": {
                    "parameters": {
                        "set_value": {
                            "severity_low": "auto",
                            "severity_medium": "approve",
                        }
                    }
                },
                "severity_rules": {"missing_required_param": "severity_medium"},
            }
        )
    )
    policy = AutonomyPolicy.load(cfg)
    assert policy.resolve("parameters", "set_value", "severity_low") == "auto"
    assert policy.resolve("parameters", "set_value", "severity_medium") == "approve"
    assert policy.resolve("unknown", "x", "y") == "approve"  # safe default
    assert policy.resolve_severity("missing_required_param") == "severity_medium"
