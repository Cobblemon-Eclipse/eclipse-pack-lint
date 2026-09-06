"""Regression expectations against the real 2026-09-05 corpus.

These are the four crashes the 1.8 cutover shipped, plus the two the audit
lanes found in the same pass. They need the real jar and pack, which are far
too big for the repo, so the test skips unless both are pointed at:

    set PACKLINT_JAR=D:\\JARS\\1.8-release\\Cobblemon-fabric-1.8.0+1.21.1.jar
    set PACKLINT_PACK=D:\\JARS\\resource_pack-V38d-unsquashed.zip
    python -m unittest tests.test_regression

CI runs the synthetic suite only; this is the local gate before a pack ships.
The synthetic equivalents of every expectation below live in test_rules.py, so
the rules themselves stay covered without the corpus.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from packlint import builtin, ownership, validate  # noqa: E402
from packlint.sources import ROLE_JAR, ROLE_PACK, EffectiveView, ZipSource  # noqa: E402

JAR = os.environ.get("PACKLINT_JAR")
PACK = os.environ.get("PACKLINT_PACK")

#: Findings that MUST appear when V38d is linted against the 1.8.0 jar.
#: Each is (rule, a substring that must appear in the finding's message).
EXPECTED = [
    # crash-2026-09-05_22.10.52 / 22.54.42 - the jar's sandshrew poser transforms
    # `ball`; the pack's stale pre-1.8 sandshrew_alolan.geo has no such bone.
    ("PL-C001", "sandshrew_alolan.geo"),
    ("PL-C001", "`ball`"),
    # Found by the Kotlin-bones lane: a pack resolver sets only `poser`, so
    # RapidashModel (root bone `rapidash`) pairs with the jar's rapidash_galar.geo.
    ("PL-C002", "rapidash"),
    # The V36 Gliscor class again: the pack's decidueye.animation.json drops
    # `hisuian_cry`, which DecidueyeHisuianModel resolves through getAnimation().
    ("PL-C004", "animation.decidueye.hisuian_cry"),
    # Legacy `bedrock(group, anim)` posers whose animation group was shadowed
    # away by a reanimodel file that keeps only ground_idle.
    ("PL-C005", "animation.cosmoem.shoulder_left"),
    ("PL-C005", "animation.ursaluna_bloodmoon.ground_walk"),
    # The residue class SPG asked for.
    ("PL-R003", "marowak_alolan"),
    ("PL-R003", "mewtwo"),
]


@unittest.skipUnless(
    JAR and PACK and os.path.isfile(JAR or "") and os.path.isfile(PACK or ""),
    "set PACKLINT_JAR and PACKLINT_PACK to run the real-corpus regression",
)
class V38dRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        view = EffectiveView([ZipSource(JAR, ROLE_JAR), ZipSource(PACK, ROLE_PACK)])
        try:
            cls.result = validate.validate(
                view,
                ownership.load(None),
                builtin_table=builtin.load_builtin("1.8.0"),
            )
        finally:
            view.close()

    def test_every_known_fault_is_reported(self) -> None:
        for rule, needle in EXPECTED:
            with self.subTest(rule=rule, needle=needle):
                hits = [
                    f
                    for f in self.result.findings
                    if f.rule == rule and needle in f.message
                ]
                self.assertTrue(hits, f"{rule} finding containing {needle!r} was not reported")

    def test_the_run_fails_the_build(self) -> None:
        self.assertGreater(self.result.blocking, 0)

    def test_the_loader_emulation_sees_a_plausible_registry(self) -> None:
        """A silent regression in the scan would show up as an empty registry
        long before it showed up as a missing finding."""
        stats = self.result.stats
        self.assertGreater(stats["models"], 1500)
        self.assertGreater(stats["posers"], 1000)
        self.assertGreater(stats["resolvers"], 1000)
        self.assertGreater(stats["animation_groups"], 1000)
        self.assertGreater(stats["builtin_posers"], 300)


if __name__ == "__main__":
    unittest.main()
