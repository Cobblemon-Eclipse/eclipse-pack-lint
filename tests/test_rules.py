"""One test per rule class, on synthetic jar/pack fixtures."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from packlint import builtin, ownership, validate  # noqa: E402
from packlint.sources import ROLE_JAR, ROLE_PACK, EffectiveView, ZipSource  # noqa: E402
from tests import fixtures as fx  # noqa: E402

BUILTIN_TABLE = {
    "cobblemon_version": "test",
    "source_file": "test",
    "posers": {
        # Stands in for a real Kotlin poser such as ZoroarkHisuianModel.
        "kotlinmon": {
            "class": "KotlinmonModel",
            "root": "kotlinmon",
            "parts": ["kotlinmon", "head", "tail"],
            "animations": [["kotlinmon", "cry"]],
        }
    },
}


class RuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_lint(self, jar_files: dict[str, str], pack_files: dict[str, str],
                 config: str | None = None) -> validate.Result:
        jar = fx.write_zip(os.path.join(self.tmp.name, "jar.jar"), jar_files)
        pack = fx.write_zip(os.path.join(self.tmp.name, "pack.zip"), pack_files)
        view = EffectiveView([ZipSource(jar, ROLE_JAR), ZipSource(pack, ROLE_PACK)])
        try:
            return validate.validate(
                view, ownership.load(config), builtin_table=BUILTIN_TABLE
            )
        finally:
            view.close()

    def rules(self, result: validate.Result) -> set[str]:
        return {f.rule for f in result.findings}

    def only(self, result: validate.Result, rule: str) -> list:
        return [f for f in result.findings if f.rule == rule]

    # -- baseline ----------------------------------------------------------

    def test_clean_pack_is_clean(self) -> None:
        result = self.run_lint(fx.minimal_jar(), {"pack.mcmeta": "{}"})
        self.assertEqual(result.blocking, 0)
        self.assertNotIn("PL-F001", self.rules(result))
        self.assertNotIn("PL-C001", self.rules(result))

    # -- FATAL -------------------------------------------------------------

    def test_f001_dangling_model_reference(self) -> None:
        """A resolver pointing at a model nothing ships. The 1.8 bulbasaur class:
        1.8 gender-split bulbasaur.geo, so our resolvers dangled."""
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/resolvers/0001_nully/radiant.json"): fx.resolver(
                "cobblemon:nully",
                [{"aspects": ["radiant"], "poser": "cobblemon:nully",
                  "model": "cobblemon:nully_gone.geo"}],
                order=1,
            )
        }
        result = self.run_lint(fx.minimal_jar(), pack)
        self.assertIn("PL-F001", self.rules(result))
        self.assertIn("PL-C007", self.rules(result))
        self.assertTrue(result.blocking)

    def test_f002_per_face_uv_geo_never_registers(self) -> None:
        """`Cobblemon geo must be BOX UV` - per-face UV means the model silently
        never enters texturedModels, which then dangles every resolver."""
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/models/0001_nully/nully.geo.json"): fx.geo(
                "nully", ["head", "ball"], per_face_uv=True
            )
        }
        result = self.run_lint(fx.minimal_jar(), pack)
        self.assertIn("PL-F002", self.rules(result))
        self.assertIn("PL-F001", self.rules(result))

    def test_f003_forward_parent_reference_aborts_model_loading(self) -> None:
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/models/0001_nully/nully.geo.json"): fx.geo(
                "nully", ["head", "ear"], parent_of={"ear": "not_declared_yet"}
            )
        }
        result = self.run_lint(fx.minimal_jar(), pack)
        found = self.only(result, "PL-F003")
        self.assertTrue(found)
        self.assertTrue(found[0].detail["aborts_all_model_loading"])

    def test_f004_and_f005_unparseable_json(self) -> None:
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/resolvers/0001_nully/broken.json"): "{not json",
            fx.a("cobblemon", "bedrock/pokemon/posers/0001_nully/broken.json"): "{also not",
        }
        rules = self.rules(self.run_lint(fx.minimal_jar(), pack))
        self.assertIn("PL-F004", rules)
        self.assertIn("PL-F005", rules)

    def test_f006_unknown_pose_type(self) -> None:
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/posers/0001_nully/nully.json"): fx.poser(
                {"standing": fx.pose(["STANDING"])}
            )
        }
        result = self.run_lint(fx.minimal_jar(), pack)
        self.assertIn("PL-F006", self.rules(result))

    # -- CRASH -------------------------------------------------------------

    def test_c001_transformed_part_missing(self) -> None:
        """Tonight's sandshrew crash in miniature: the JAR poser transforms
        `ball`, the PACK's geo replacement does not have it."""
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/models/0001_nully/nully.geo.json"): fx.geo(
                "nully", ["head", "leg_left", "leg_right"]  # no `ball`
            )
        }
        result = self.run_lint(fx.minimal_jar(), pack)
        found = self.only(result, "PL-C001")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].detail["missing_bone"], "ball")
        self.assertTrue(result.blocking)

    def test_c001_root_bone_itself_is_not_a_part(self) -> None:
        """A JSON poser registers the root only as `__root`, never by its own
        name (JsonModelAdapter.kt:31) - a real source of surprise crashes."""
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/posers/0001_nully/nully.json"): fx.poser(
                {"standing": fx.pose(["STAND"], transformed=["nully"])}
            )
        }
        result = self.run_lint(fx.minimal_jar(), pack)
        self.assertIn("PL-C001", self.rules(result))

    def test_a_fault_the_pack_did_not_cause_is_not_reported(self) -> None:
        """The jar's own poser naming a bone the jar's own model lacks is
        Cobblemon's business, not a pack operator's."""
        jar = fx.minimal_jar()
        jar[fx.a("cobblemon", "bedrock/pokemon/posers/0001_nully/nully.json")] = fx.poser(
            {"standing": fx.pose(["STAND"], transformed=["nully"])}
        )
        result = self.run_lint(jar, {"pack.mcmeta": "{}"})
        self.assertNotIn("PL-C001", self.rules(result))

    def test_c002_builtin_root_bone_missing(self) -> None:
        """Tonight's zoroark crash: the pack geo kept the 1.7.3 root-bone name,
        1.8's Kotlin model asks for the new one."""
        jar = fx.minimal_jar()
        jar[fx.a("cobblemon", "bedrock/pokemon/models/kotlinmon.geo.json")] = fx.geo(
            "kotlinmon", ["head", "tail"]
        )
        jar[fx.a("cobblemon", "bedrock/pokemon/resolvers/kotlinmon.json")] = fx.resolver(
            "cobblemon:kotlinmon",
            [{"aspects": [], "poser": "cobblemon:kotlinmon", "model": "cobblemon:kotlinmon.geo"}],
        )
        jar[fx.a("cobblemon", "bedrock/pokemon/animations/kotlinmon.animation.json")] = (
            fx.animation_group("kotlinmon", ["cry"])
        )
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/models/kotlinmon.geo.json"): fx.geo(
                "kotlinmon_old_name", ["head", "tail"]
            )
        }
        result = self.run_lint(jar, pack)
        found = self.only(result, "PL-C002")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].detail["missing_bone"], "kotlinmon")

    def test_c002_independent_poser_and_model_resolution(self) -> None:
        """Tonight's rapidash crash: a pack resolver sets ONLY `poser`, so it
        pairs with whatever model a different variation supplies."""
        jar = fx.minimal_jar()
        jar[fx.a("cobblemon", "bedrock/pokemon/models/kotlinmon_form.geo.json")] = fx.geo(
            "kotlinmon_form", ["head", "tail"]
        )
        jar[fx.a("cobblemon", "bedrock/pokemon/animations/kotlinmon.animation.json")] = (
            fx.animation_group("kotlinmon", ["cry"])
        )
        jar[fx.a("cobblemon", "bedrock/pokemon/resolvers/kotlinmon/0_base.json")] = fx.resolver(
            "cobblemon:kotlinmon",
            [{"aspects": ["form"], "poser": "cobblemon:form_poser",
              "model": "cobblemon:kotlinmon_form.geo"}],
        )
        jar[fx.a("cobblemon", "bedrock/pokemon/posers/kotlinmon/form_poser.json")] = fx.poser(
            {"standing": fx.pose(["STAND"])}, root_bone="kotlinmon_form"
        )
        # The pack contributes a texture-only variation that ALSO swaps the poser
        # to the Kotlin one, without swapping the model.
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/resolvers/kotlinmon/1_event.json"): fx.resolver(
                "cobblemon:kotlinmon",
                [{"aspects": ["event"], "poser": "cobblemon:kotlinmon"}],
                order=1,
            )
        }
        result = self.run_lint(jar, pack)
        found = self.only(result, "PL-C002")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].detail["model"], "cobblemon:kotlinmon_form.geo")
        self.assertEqual(found[0].detail["missing_bone"], "kotlinmon")

    def test_c003_builtin_part_missing(self) -> None:
        jar = fx.minimal_jar()
        jar[fx.a("cobblemon", "bedrock/pokemon/models/kotlinmon.geo.json")] = fx.geo(
            "kotlinmon", ["head", "tail"]
        )
        jar[fx.a("cobblemon", "bedrock/pokemon/animations/kotlinmon.animation.json")] = (
            fx.animation_group("kotlinmon", ["cry"])
        )
        jar[fx.a("cobblemon", "bedrock/pokemon/resolvers/kotlinmon.json")] = fx.resolver(
            "cobblemon:kotlinmon",
            [{"aspects": [], "poser": "cobblemon:kotlinmon", "model": "cobblemon:kotlinmon.geo"}],
        )
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/models/kotlinmon.geo.json"): fx.geo(
                "kotlinmon", ["head"]  # `tail` removed
            )
        }
        result = self.run_lint(jar, pack)
        found = self.only(result, "PL-C003")
        self.assertEqual([f.detail["missing_bone"] for f in found], ["tail"])

    def test_c004_shadowing_animation_group_drops_a_hard_reference(self) -> None:
        """The V36 Gliscor class, and tonight's decidueye: the pack's animation
        group wins the file-stem key and is missing an animation the Kotlin
        model resolves through getAnimation(), which throws."""
        jar = fx.minimal_jar()
        jar[fx.a("cobblemon", "bedrock/pokemon/models/kotlinmon.geo.json")] = fx.geo(
            "kotlinmon", ["head", "tail"]
        )
        jar[fx.a("cobblemon", "bedrock/pokemon/animations/0999_k/kotlinmon.animation.json")] = (
            fx.animation_group("kotlinmon", ["cry", "ground_idle"])
        )
        jar[fx.a("cobblemon", "bedrock/pokemon/resolvers/kotlinmon.json")] = fx.resolver(
            "cobblemon:kotlinmon",
            [{"aspects": [], "poser": "cobblemon:kotlinmon", "model": "cobblemon:kotlinmon.geo"}],
        )
        pack = {
            # Different directory, same file stem -> replaces the group wholesale.
            fx.a("cobblemon", "bedrock/pokemon/animations/kotlinmon/kotlinmon.animation.json"):
                fx.animation_group("kotlinmon", ["ground_idle"])
        }
        result = self.run_lint(jar, pack)
        self.assertIn("PL-C004", self.rules(result))
        self.assertIn("PL-R002", self.rules(result))
        self.assertIn("PL-R003", self.rules(result))
        lost = self.only(result, "PL-R003")[0]
        self.assertEqual(lost.detail["lost"], ["animation.kotlinmon.cry"])

    def test_c005_legacy_bedrock_animation_form_throws(self) -> None:
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/posers/0001_nully/nully.json"): fx.poser(
                {"standing": fx.pose(["STAND"], animations=["bedrock(nully, nope)"])}
            )
        }
        result = self.run_lint(fx.minimal_jar(), pack)
        self.assertIn("PL-C005", self.rules(result))

    def test_c006_quirk_reads_only_the_outer_function(self) -> None:
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/posers/0001_nully/nully.json"): fx.poser(
                {
                    "ok": fx.pose(
                        ["STAND"],
                        quirks=["q.bedrock_quirk('nully', q.array('blink'), 8, 30)"],
                    ),
                    "bad": fx.pose(["WALK"], quirks=["q.curve('symmetrical_wide')"]),
                }
            )
        }
        result = self.run_lint(fx.minimal_jar(), pack)
        found = self.only(result, "PL-C006")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].detail["pose"], "bad")

    # -- WARN --------------------------------------------------------------

    def test_w001_missing_poser_falls_back_to_substitute(self) -> None:
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/resolvers/0001_nully/1_x.json"): fx.resolver(
                "cobblemon:nully",
                [{"aspects": ["x"], "poser": "cobblemon:nope", "model": "cobblemon:nully.geo"}],
                order=1,
            )
        }
        self.assertIn("PL-W001", self.rules(self.run_lint(fx.minimal_jar(), pack)))

    def test_w002_molang_animation_miss_is_only_a_warning(self) -> None:
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/posers/0001_nully/nully.json"): fx.poser(
                {"standing": fx.pose(["STAND"], animations=["q.bedrock('nully', 'nope')"])}
            )
        }
        result = self.run_lint(fx.minimal_jar(), pack)
        self.assertIn("PL-W002", self.rules(result))
        self.assertNotIn("PL-C005", self.rules(result))
        self.assertEqual(result.blocking, 0)

    def test_w003_missing_texture(self) -> None:
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/resolvers/0001_nully/1_x.json"): fx.resolver(
                "cobblemon:nully",
                [{"aspects": ["x"], "texture": "cobblemon:textures/pokemon/nully/gone.png"}],
                order=1,
            )
        }
        self.assertIn("PL-W003", self.rules(self.run_lint(fx.minimal_jar(), pack)))

    def test_w004_and_w009_filename_key_collisions(self) -> None:
        pack = {
            # Same scanned directory, different subdir -> undefined winner.
            fx.a("cobblemon", "bedrock/pokemon/models/dupe_a/nully.geo.json"): fx.geo(
                "nully", ["head", "ball"]
            ),
            # An earlier-scanned directory always loses -> inert.
            fx.a("cobblemon", "bedrock/models/nully.geo.json"): fx.geo("nully", ["head"]),
        }
        rules = self.rules(self.run_lint(fx.minimal_jar(), pack))
        self.assertIn("PL-W004", rules)
        self.assertIn("PL-W009", rules)

    def test_w005_and_w006_hygiene(self) -> None:
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/posers/0001_nully/nully.json.bak"): "{}",
            ".DS_Store": "junk",
            "data/cobblemon/species/nully.json": "{}",
        }
        rules = self.rules(self.run_lint(fx.minimal_jar(), pack))
        self.assertIn("PL-W005", rules)
        self.assertIn("PL-W006", rules)

    def test_w007_animation_bone_reference(self) -> None:
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/posers/0001_nully/nully.json"): fx.poser(
                {"standing": fx.pose(["STAND"], animations=["q.look('head_ai')"])}
            )
        }
        self.assertIn("PL-W007", self.rules(self.run_lint(fx.minimal_jar(), pack)))

    def test_w008_declared_root_bone_is_ignored(self) -> None:
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/posers/0001_nully/nully.json"): fx.poser(
                {"standing": fx.pose(["STAND"])}, root_bone="not_a_bone"
            )
        }
        self.assertIn("PL-W008", self.rules(self.run_lint(fx.minimal_jar(), pack)))

    # -- RESIDUE -----------------------------------------------------------

    def test_r001_same_path_override(self) -> None:
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/models/0001_nully/nully.geo.json"): fx.geo(
                "nully", ["head", "ball", "leg_left", "leg_right"]
            )
        }
        result = self.run_lint(fx.minimal_jar(), pack)
        found = self.only(result, "PL-R001")
        self.assertEqual(len(found), 1)
        self.assertEqual(
            found[0].suggest_delete,
            [fx.a("cobblemon", "bedrock/pokemon/models/0001_nully/nully.geo.json")],
        )

    def test_r002_key_shadow_from_a_different_path(self) -> None:
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/models/ours/nully.geo.json"): fx.geo(
                "nully", ["head", "ball", "leg_left", "leg_right"]
            )
        }
        self.assertIn("PL-R002", self.rules(self.run_lint(fx.minimal_jar(), pack)))

    def test_residue_respects_the_ownership_config(self) -> None:
        pack = {
            fx.a("cobblemon", "bedrock/pokemon/models/0001_nully/nully.geo.json"): fx.geo(
                "nully", ["head", "ball", "leg_left", "leg_right"]
            ),
            fx.a("cobblemon", "bedrock/pokemon/models/radiant/radiant_nully.geo.json"): fx.geo(
                "nully", ["head", "ball"]
            ),
        }
        config = os.path.join(self.tmp.name, "owned.json")
        with open(config, "w", encoding="utf-8") as fh:
            json.dump({"aspects": ["radiant"], "path_globs": ["assets/*/bedrock/pokemon/models/0001_nully/*"]}, fh)
        result = self.run_lint(fx.minimal_jar(), pack, config=config)
        self.assertEqual(self.only(result, "PL-R001"), [])
        self.assertEqual(self.only(result, "PL-R002"), [])

    # -- calibration -------------------------------------------------------

    def test_vanilla_baseline_suppresses_jar_only_faults(self) -> None:
        """If the jar alone is already missing an animation, that is not the
        pack's doing and must not be reported."""
        jar = fx.minimal_jar()
        jar[fx.a("cobblemon", "bedrock/pokemon/models/kotlinmon.geo.json")] = fx.geo(
            "kotlinmon", ["head", "tail"]
        )
        jar[fx.a("cobblemon", "bedrock/pokemon/resolvers/kotlinmon.json")] = fx.resolver(
            "cobblemon:kotlinmon",
            [{"aspects": [], "poser": "cobblemon:kotlinmon", "model": "cobblemon:kotlinmon.geo"}],
        )
        # No kotlinmon.animation.json anywhere - vanilla is equally broken.
        result = self.run_lint(jar, {"pack.mcmeta": "{}"})
        self.assertNotIn("PL-C004", self.rules(result))
        self.assertTrue(any(s["rule"] == "PL-C004" for s in result.suppressed))


class PairingTest(unittest.TestCase):
    """`reachable_pairs` is the piece that made tonight's rapidash crash
    findable, so it gets its own unit tests."""

    def _resolver(self, entries: list[tuple[list[str], str | None, str | None]]):
        from packlint.loader import Resolver, Variation

        r = Resolver("cobblemon:x")
        for aspects, poser, model in entries:
            r.variations.append(
                Variation(aspects, poser, model, None, [], {}, "p", "s")
            )
        return r

    def test_poser_and_model_resolve_independently(self) -> None:
        r = self._resolver(
            [
                ([], "p_base", "m_base"),
                (["form"], None, "m_form"),
                (["event"], "p_event", None),
            ]
        )
        pairs = validate.reachable_pairs(r)
        # The event variation's poser can land on the form's model.
        self.assertIn(("p_event", "m_form"), pairs)
        self.assertIn(("p_base", "m_base"), pairs)
        self.assertIn(("p_base", "m_form"), pairs)

    def test_a_later_variation_masks_an_earlier_one(self) -> None:
        r = self._resolver([([], "p_base", "m_base"), ([], "p_late", "m_late")])
        pairs = validate.reachable_pairs(r)
        self.assertEqual(pairs, {("p_late", "m_late")})


class BuiltinExtractionTest(unittest.TestCase):
    def test_comments_are_stripped(self) -> None:
        text = '''
        val a = bedrockStateful("real", "cry")
        // val b = bedrockStateful("commented", "faint")
        /* val c = bedrockStateful("blocked", "faint") */
        val d = "a // not a comment"
        '''
        stripped = builtin.strip_comments(text)
        self.assertIn('bedrockStateful("real", "cry")', stripped)
        self.assertNotIn("commented", stripped)
        self.assertNotIn("blocked", stripped)
        self.assertIn("a // not a comment", stripped)

    def test_shipped_table_loads_and_looks_sane(self) -> None:
        table = builtin.load_builtin("1.8.0")
        self.assertGreater(len(table["posers"]), 300)
        self.assertEqual(table["posers"]["sandslash"]["root"], "sandslash")
        self.assertIn("head", table["posers"]["sandslash"]["parts"])


if __name__ == "__main__":
    unittest.main()
