"""Grouped, colour-free report rendering.

Colour-free on purpose: this output ends up in CI logs, in a Bloom console, and
pasted into Discord, none of which handle ANSI well.
"""

from __future__ import annotations

import textwrap
from typing import Any

from packlint import findings as F
from packlint.validate import Result

WIDTH = 96


def _wrap(text: str, indent: str = "      ") -> str:
    return textwrap.fill(
        text, width=WIDTH, initial_indent=indent, subsequent_indent=indent
    )


def render(result: Result, verbose: bool = False, limit: int = 25) -> str:
    lines: list[str] = []
    stats = result.stats

    lines.append("=" * WIDTH)
    lines.append("eclipse-pack-lint")
    lines.append("=" * WIDTH)
    for source in stats.get("sources", []):
        lines.append(f"  source   {source}")
    lines.append(
        "  loaded   {models} models, {posers} JSON posers (+{builtin} built-in), "
        "{resolvers} species / {variations} variations, {anims} animation groups".format(
            models=stats.get("models", 0),
            posers=stats.get("posers", 0),
            builtin=stats.get("builtin_posers", 0),
            resolvers=stats.get("resolvers", 0),
            variations=stats.get("variations", 0),
            anims=stats.get("animation_groups", 0),
        )
    )
    table = stats.get("builtin_table")
    if table:
        lines.append(
            f"  builtin  {table} (Cobblemon {stats.get('builtin_table_version')})"
        )
    lines.append("")

    counts = result.counts()
    lines.append("SUMMARY")
    for severity in F.SEVERITY_ORDER:
        lines.append(
            f"  {severity:<8} {counts.get(severity, 0):>6}   {F.SEVERITY_BLURB[severity]}"
        )
    if result.suppressed:
        lines.append(
            f"  {'(suppr)':<8} {len(result.suppressed):>6}   "
            "detections the vanilla-jar baseline rules out (see --json)"
        )
    lines.append("")

    by_rule: dict[str, list[F.Finding]] = {}
    for finding in result.findings:
        by_rule.setdefault(finding.rule, []).append(finding)

    for severity in F.SEVERITY_ORDER:
        rules = [
            rid
            for rid in F.RULES
            if F.RULES[rid].severity == severity and rid in by_rule
        ]
        if not rules:
            continue
        lines.append("-" * WIDTH)
        lines.append(f"{severity}  ({counts.get(severity, 0)})")
        lines.append("-" * WIDTH)
        for rule_id in rules:
            rule = F.RULES[rule_id]
            group = by_rule[rule_id]
            total = stats.get("rule_totals", {}).get(rule_id, len(group))
            header = f"  [{rule_id}] {rule.title}  x{total}"
            lines.append(header)
            shown = group if verbose else group[:limit]
            for finding in shown:
                lines.append(_wrap(f"- {finding.message}", indent="    "))
                if finding.path and finding.path not in finding.message:
                    lines.append(f"        in {finding.path}")
            if len(group) > len(shown):
                lines.append(
                    f"        ... and {len(group) - len(shown)} more "
                    "(use --verbose or --json)"
                )
            if total > len(group):
                lines.append(
                    f"        ... {total - len(group)} further occurrences were not "
                    "collected (--max-per-rule)"
                )
            lines.append(f"        why: {rule.effect.splitlines()[0][:WIDTH - 14]}")
            lines.append(f"        fix: {rule.remedy.splitlines()[0][:WIDTH - 14]}")
            lines.append(f"        (packlint validate --explain {rule_id})")
            lines.append("")

    lines.append("=" * WIDTH)
    if result.blocking:
        lines.append(
            f"FAIL - {counts.get(F.FATAL, 0)} FATAL + {counts.get(F.CRASH, 0)} CRASH. "
            "Do NOT ship this pack."
        )
    elif counts.get(F.WARN, 0) or counts.get(F.RESIDUE, 0):
        lines.append("PASS - no load-breaking or crash-class findings. Review the WARN/RESIDUE list.")
    else:
        lines.append("PASS - clean.")
    lines.append("=" * WIDTH)
    return "\n".join(lines)


def explain(rule_id: str) -> str:
    rule = F.RULES.get(rule_id.upper())
    if rule is None:
        known = ", ".join(sorted(F.RULES))
        return f"Unknown rule id {rule_id!r}.\nKnown rules: {known}"
    out = [
        "=" * WIDTH,
        f"{rule.id}  [{rule.severity}]  {rule.title}",
        "=" * WIDTH,
        "",
        "WHAT HAPPENS",
        _wrap(rule.effect, indent="  "),
        "",
        "SOURCE RULE (Cobblemon 1.8.0)",
        _wrap(rule.citation, indent="  "),
        "",
        "HOW TO FIX",
        _wrap(rule.remedy, indent="  "),
        "",
        "Paths are relative to common/src/main/kotlin/com/cobblemon/mod/common/.",
    ]
    return "\n".join(out)


def to_json(result: Result) -> dict[str, Any]:
    return {
        "summary": result.counts(),
        "blocking": result.blocking,
        "stats": result.stats,
        "findings": [f.to_json() for f in result.findings],
        "suppressed": result.suppressed,
        "rules": {
            rid: {
                "severity": r.severity,
                "title": r.title,
                "effect": r.effect,
                "citation": r.citation,
                "remedy": r.remedy,
            }
            for rid, r in F.RULES.items()
        },
    }
