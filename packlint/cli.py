"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys

from packlint import __version__, builtin, fixer, ownership, report, validate
from packlint.sources import ROLE_JAR, ROLE_PACK, EffectiveView, open_include_dir, open_source


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="packlint",
        description=(
            "Static validator for Cobblemon client resource packs. Emulates Cobblemon's "
            "asset loader against a jar + pack overlay and reports what would break."
        ),
    )
    parser.add_argument("--version", action="version", version=f"packlint {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="lint a pack (or an include dir) against a jar")
    v.add_argument("--jar", help="the Cobblemon jar the pack will run against")
    v.add_argument(
        "--pack",
        action="append",
        default=[],
        metavar="ZIP_OR_DIR",
        help="a resource pack; repeat for a stack, later wins",
    )
    v.add_argument(
        "--include-dir",
        metavar="DIR",
        help=(
            "a polymer include directory; every .zip in it is merged the way "
            "`polymer generate-pack` would (later filename wins), so you can lint BEFORE "
            "generating"
        ),
    )
    v.add_argument("--config", metavar="JSON", help="eclipse-owned.json (residue exemptions)")
    v.add_argument("--json", metavar="FILE", help="write the full machine-readable report")
    v.add_argument(
        "--fix-manifest",
        metavar="FILE",
        help="write a {delete, write} manifest of the residue this run found",
    )
    v.add_argument(
        "--fix-manifest-severity",
        default="RESIDUE",
        help="comma-separated severities to include in --fix-manifest (default RESIDUE)",
    )
    v.add_argument("--builtin-bones", metavar="JSON", help="override the built-in poser table")
    v.add_argument("--explain", metavar="RULE_ID", help="print a rule's source citation and exit")
    v.add_argument("--verbose", action="store_true", help="list every finding, not the first 25")
    v.add_argument(
        "--limit", type=int, default=25, help="findings printed per rule (default 25)"
    )
    v.add_argument(
        "--max-per-rule",
        type=int,
        default=0,
        help="stop collecting a rule after N findings (0 = unlimited)",
    )
    v.add_argument(
        "--warn-exit",
        action="store_true",
        help="also exit non-zero when there are WARN findings",
    )
    v.add_argument(
        "--residue-exit",
        action="store_true",
        help="also exit non-zero when there are RESIDUE findings",
    )

    e = sub.add_parser(
        "extract-builtin",
        help="regenerate the built-in poser bone table from the Cobblemon Kotlin source",
    )
    e.add_argument("--source", required=True, help="Cobblemon source tree (or its common/ dir)")
    e.add_argument("--out", help="output path (default packlint/data/builtin_bones_<ver>.json)")

    f = sub.add_parser("fix", help="apply a {delete, write} manifest to a pack, into a NEW zip")
    f.add_argument("--pack", required=True, help="the pack zip to transform")
    f.add_argument("--manifest", required=True, help="the manifest JSON")
    f.add_argument("--out", required=True, help="the new zip to write (must not exist)")

    sub.add_parser("rules", help="list every rule with its severity")

    return parser


def _open_sources(args: argparse.Namespace) -> list:
    sources = []
    if args.jar:
        sources.append(open_source(args.jar, ROLE_JAR))
    if args.include_dir:
        sources.extend(open_include_dir(args.include_dir))
    for pack in args.pack:
        sources.append(open_source(pack, ROLE_PACK))
    return sources


def cmd_validate(args: argparse.Namespace) -> int:
    if args.explain:
        print(report.explain(args.explain))
        return 0
    if not args.pack and not args.include_dir:
        print("error: pass at least one --pack or an --include-dir", file=sys.stderr)
        return 2

    sources = _open_sources(args)
    view = EffectiveView(sources)
    if not view.jar_sources():
        print(
            "warning: no --jar given; FATAL/CRASH pairing checks and the whole RESIDUE class "
            "need the jar to compare against",
            file=sys.stderr,
        )

    owned = ownership.load(args.config)
    jar_version = validate.detect_jar_version(view)
    table = (
        builtin.load_builtin(path=args.builtin_bones)
        if args.builtin_bones
        else builtin.load_builtin(jar_version)
    )
    result = validate.validate(
        view, owned, builtin_table=table, max_per_rule=args.max_per_rule
    )
    result.stats["jar_version"] = jar_version
    result.stats["ownership_config"] = owned.source

    print(report.render(result, verbose=args.verbose, limit=args.limit))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report.to_json(result), fh, indent=2)
        print(f"\nwrote {args.json}")

    if args.fix_manifest:
        severities = tuple(
            s.strip().upper() for s in args.fix_manifest_severity.split(",") if s.strip()
        )
        manifest = fixer.build_manifest(result, include=severities)
        with open(args.fix_manifest, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
        print(f"wrote {args.fix_manifest} ({len(manifest['delete'])} deletions proposed)")
        if "RESIDUE" in severities:
            print(
                "  NOTE: residue deletions are suggestions, not a safe bulk apply.\n"
                "  Deleting our copy restores the jar's, which may not satisfy a poser or\n"
                "  resolver we still ship. Apply in slices and re-lint after each one."
            )

    counts = result.counts()
    if result.blocking:
        return 1
    if args.warn_exit and counts.get("WARN"):
        return 1
    if args.residue_exit and counts.get("RESIDUE"):
        return 1
    return 0


def cmd_extract_builtin(args: argparse.Namespace) -> int:
    data = builtin.extract(args.source)
    out = args.out or os.path.join(
        builtin.DATA_DIR, f"builtin_bones_{data['cobblemon_version']}.json"
    )
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    print(
        f"extracted {data['poser_count']} built-in posers "
        f"(Cobblemon {data['cobblemon_version']}) -> {out}"
    )
    return 0


def cmd_fix(args: argparse.Namespace) -> int:
    manifest = fixer.load_manifest(args.manifest)
    outcome = fixer.apply_manifest(args.pack, manifest, args.out)
    print(f"wrote {outcome['out']}")
    print(f"  deleted  {len(outcome['deleted'])}")
    print(f"  replaced {len(outcome['replaced'])}")
    print(f"  added    {len(outcome['added'])}")
    if outcome["delete_not_found"]:
        print("  NOT FOUND (manifest lists, pack does not contain):")
        for path in outcome["delete_not_found"]:
            print(f"    {path}")
    print(
        "\nRe-lint the result before shipping it - a deletion can restore a jar asset\n"
        "that does not satisfy a poser or resolver the pack still carries:\n"
        f"  python -m packlint validate --jar <cobblemon.jar> --pack {outcome['out']}"
    )
    return 0


def cmd_rules(_args: argparse.Namespace) -> int:
    from packlint import findings as F

    for severity in F.SEVERITY_ORDER:
        print(f"{severity}  - {F.SEVERITY_BLURB[severity]}")
        for rule_id, rule in F.RULES.items():
            if rule.severity == severity:
                print(f"  {rule_id}  {rule.title}")
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "extract-builtin":
        return cmd_extract_builtin(args)
    if args.command == "fix":
        return cmd_fix(args)
    if args.command == "rules":
        return cmd_rules(args)
    parser.print_help()
    return 2
