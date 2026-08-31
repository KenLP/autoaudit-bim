"""CLI for IDS <-> rules.yaml conversion.

Usage
-----
  # Export: rules YAML -> IDS XML
  python scripts/ids_convert.py --export config/rules.room_compliance.yaml
  python scripts/ids_convert.py --export config/rules.room_compliance.yaml -o output.ids

  # Import: IDS XML -> rules YAML (stdout or file)
  python scripts/ids_convert.py --import-ids rules.ids
  python scripts/ids_convert.py --import-ids rules.ids -o config/rules.imported.yaml

For integration with bim-orchestrator, use:
  uv run python scripts/ids_convert.py --export config/rules.room_compliance.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

# Allow running from repo root without installing
_REPO = Path(__file__).resolve().parents[1]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from bim_orchestrator.agents.qc import RuleSet
from bim_orchestrator.policies.ids_converter import (
    CATEGORY_TO_IFC,
    IFC_TO_CATEGORY,
    ids_xml_to_ruleset,
    ruleset_to_ids_xml,
)


def _load_ruleset(path: Path) -> RuleSet:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return RuleSet.model_validate(data)


def cmd_export(args: argparse.Namespace) -> int:
    rules_path = Path(args.rules_file)
    if not rules_path.exists():
        print(f"error: file not found: {rules_path}", file=sys.stderr)
        return 1
    ruleset = _load_ruleset(rules_path)
    xml, warnings = ruleset_to_ids_xml(
        ruleset,
        title=args.title or None,
        description=args.description or None,
        version=args.ids_version,
        ifc_versions=args.ifc_versions,
    )
    if warnings:
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
    if args.output:
        out = Path(args.output)
        out.write_text(xml, encoding="utf-8")
        print(f"exported {len(ruleset.rules)} rules -> {out}", file=sys.stderr)
    else:
        print(xml)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    ids_path = Path(args.ids_file)
    if not ids_path.exists():
        print(f"error: file not found: {ids_path}", file=sys.stderr)
        return 1
    xml_text = ids_path.read_text(encoding="utf-8")
    ruleset, warnings = ids_xml_to_ruleset(xml_text)
    if warnings:
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)

    # Serialise back to YAML
    data = ruleset.model_dump(exclude_none=True)
    yaml_text = yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)

    if args.output:
        out = Path(args.output)
        out.write_text(yaml_text, encoding="utf-8")
        print(f"imported {len(ruleset.rules)} rules -> {out}", file=sys.stderr)
    else:
        print(yaml_text)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="ids_convert",
        description="buildingSMART IDS 1.0 <-> bim-orchestrator rules.yaml converter",
    )
    sub = parser.add_subparsers(dest="cmd")

    # -- export ---
    exp = sub.add_parser("export", help="Export rules YAML to IDS XML")
    exp.add_argument("rules_file", help="Path to rules.*.yaml")
    exp.add_argument("-o", "--output", help="Output .ids file (default: stdout)")
    exp.add_argument("--title", default="", help="Override IDS title (default: scenario name)")
    exp.add_argument("--description", default="", help="IDS description text")
    exp.add_argument("--ids-version", default="1.0", help="IDS document version (default: 1.0)")
    exp.add_argument(
        "--ifc-versions",
        default="IFC2X3 IFC4 IFC4X3_ADD2",
        help="Space-separated IFC versions (default: IFC2X3 IFC4 IFC4X3_ADD2)",
    )

    # -- import --
    imp = sub.add_parser("import-ids", help="Import IDS XML to rules YAML")
    imp.add_argument("ids_file", help="Path to .ids file")
    imp.add_argument("-o", "--output", help="Output rules YAML file (default: stdout)")

    # Backwards-compat: also accept --export / --import-ids as flags (legacy style)
    parser.add_argument("--export", metavar="RULES_FILE", help=argparse.SUPPRESS)
    parser.add_argument("--import-ids", metavar="IDS_FILE", dest="import_ids_flag",
                        help=argparse.SUPPRESS)
    parser.add_argument("-o", "--output", help=argparse.SUPPRESS)
    parser.add_argument("--title", default="", help=argparse.SUPPRESS)
    parser.add_argument("--description", default="", help=argparse.SUPPRESS)
    parser.add_argument("--ids-version", default="1.0", help=argparse.SUPPRESS)
    parser.add_argument(
        "--ifc-versions", default="IFC2X3 IFC4 IFC4X3_ADD2", help=argparse.SUPPRESS
    )

    args = parser.parse_args()

    # Handle legacy --export / --import-ids flags
    if getattr(args, "export", None):
        args.rules_file = args.export
        args.cmd = "export"
    if getattr(args, "import_ids_flag", None):
        args.ids_file = args.import_ids_flag
        args.cmd = "import-ids"

    if not args.cmd:
        parser.print_help()
        return 0

    if args.cmd == "export":
        return cmd_export(args)
    if args.cmd == "import-ids":
        return cmd_import(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
