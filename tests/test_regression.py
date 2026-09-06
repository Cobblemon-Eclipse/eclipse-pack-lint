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
#: Optional: the two earlier packs from the same night. Each one was live for a
#: different crash report, so linting them proves the tool reproduces a crash it
#: was not written against.
PACK_V38C = os.environ.get("PACKLINT_PACK_V38C")
PACK_ORIGINAL = os.environ.get("PACKLINT_PACK_ORIGINAL")


def _lint(jar: str, pack: str) -> validate.Result:
    view = EffectiveView([ZipSource(jar, ROLE_JAR), ZipSource(pack, ROLE_PACK)])
    try:
        return validate.validate(
            view, ownership.load(None), builtin_table=builtin.load_builtin("1.8.0")
        )
    finally:
        view.close()

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
        cls.result = _lint(JAR, PACK)

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


@unittest.skipUnless(
    JAR and PACK_V38C and os.path.isfile(JAR or "") and os.path.isfile(PACK_V38C or ""),
    "set PACKLINT_PACK_V38C to run the V38c regression",
)
class V38cRegressionTest(unittest.TestCase):
    """V38c was live for crash-2026-09-05_21.15.07.

    `java.util.NoSuchElementException: Can't find part zoroark` from
    ZoroarkHisuianModel's constructor: the pack's gilded_zoroark_hisuian.geo
    still carried the 1.7.3-era root bone name `zoroark_hisuian`, and 1.8's
    Kotlin model asks for `zoroark`.
    """

    def test_zoroark_root_bone_crash_is_reported(self) -> None:
        result = _lint(JAR, PACK_V38C)
        hits = [
            f
            for f in result.findings
            if f.rule == "PL-C002"
            and f.detail.get("missing_bone") == "zoroark"
            and "zoroark_hisuian" in f.detail.get("model", "")
        ]
        self.assertTrue(hits, "the V38c zoroark root-bone crash was not reported")


@unittest.skipUnless(
    JAR
    and PACK_ORIGINAL
    and os.path.isfile(JAR or "")
    and os.path.isfile(PACK_ORIGINAL or ""),
    "set PACKLINT_PACK_ORIGINAL to run the first-pack regression",
)
class OriginalPackRegressionTest(unittest.TestCase):
    """The first 1.8 pack was live for crash-2026-09-05_20.25.27 / 20.30.56.

    `UninitializedPropertyAccessException: lateinit property repository has not
    been initialized` - the cascade of a dangling `cobblemon:bulbasaur.geo`
    reference (1.8 gender-split bulbasaur into bulbasaur_male/female.geo), which
    aborts registerVariations for every species after it.
    """

    def test_dangling_bulbasaur_model_is_fatal_and_cascades(self) -> None:
        result = _lint(JAR, PACK_ORIGINAL)
        fatal = [
            f
            for f in result.findings
            if f.rule == "PL-F001" and f.detail.get("model") == "cobblemon:bulbasaur.geo"
        ]
        self.assertTrue(fatal, "the dangling bulbasaur.geo reference was not reported")
        self.assertTrue(
            [f for f in result.findings if f.rule == "PL-C007"],
            "the uninitialised-repository cascade was not reported",
        )


if __name__ == "__main__":
    unittest.main()
