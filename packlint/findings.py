"""Finding model + the rule registry.

Every rule carries the Cobblemon source citation it was derived from, so that a
future Cobblemon version can be re-verified rule by rule (see README, "Updating
for a new Cobblemon version"). `packlint validate --explain <RULE-ID>` prints
the citation.

Source paths are relative to the Cobblemon common source root:
    common/src/main/kotlin/com/cobblemon/mod/common/
Line numbers are for Cobblemon 1.8.0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

BLOCKBENCH = "client/render/models/blockbench"

# Severity classes, most severe first. `validate` exits non-zero on FATAL/CRASH.
FATAL = "FATAL"
CRASH = "CRASH"
WARN = "WARN"
RESIDUE = "RESIDUE"

SEVERITY_ORDER = [FATAL, CRASH, WARN, RESIDUE]

SEVERITY_BLURB = {
    FATAL: "the pack fails to load for everyone - no Pokemon renders",
    CRASH: "the client crashes when the affected model renders",
    WARN: "renders, but degraded (fallback poser, T-pose, missing texture)",
    RESIDUE: "pack asset shadowing an asset the Cobblemon jar now ships itself",
}


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    title: str
    #: What actually happens in game.
    effect: str
    #: Source citation: file + line + the expression that blows up.
    citation: str
    #: How an operator fixes it.
    remedy: str


RULES: dict[str, Rule] = {}


def _rule(*args: Any) -> Rule:
    r = Rule(*args)
    RULES[r.id] = r
    return r


# --------------------------------------------------------------------------
# FATAL - registerModels / registerVariations abort; nothing renders.
# --------------------------------------------------------------------------

MODEL_MISSING = _rule(
    "PL-F001",
    FATAL,
    "resolver variation.model has no effective model",
    "VaryingRenderableResolver.initialize() throws while wiring this species, which "
    "aborts the `variations.values.forEach { it.initialize(this) }` loop in "
    "registerVariations. Every species after this one in map order is left with an "
    "uninitialised `lateinit var repository`, so the first Pokemon rendered dies with "
    "`kotlin.UninitializedPropertyAccessException: lateinit property repository has not "
    "been initialized`. This one fault takes down rendering for the whole server.",
    f"{BLOCKBENCH}/repository/VaryingModelRepository.kt:597-604 (registerVariations) -> "
    "client/render/VaryingRenderableResolver.kt:110 "
    "`models[identifier] = repository.texturedModels[identifier]!!`",
    "Either ship the referenced .geo.json in a modelDirectory, or point the variation at a "
    "model key that exists. Model keys are `namespace:<filename minus .json>`, e.g. "
    "`cobblemon:sandshrew.geo` for `sandshrew.geo.json`.",
)

GEO_UNLOADABLE = _rule(
    "PL-F002",
    FATAL,
    "geo file cannot be deserialized by TexturedModel - model never registers",
    "TexturedModel.from() swallows the exception and returns null, so the model is simply "
    "absent from `texturedModels`. The file looks present on disk but is invisible to the "
    "loader; any resolver pointing at it then trips PL-F001. The classic cause is per-face "
    "UV (`\"uv\": {\"north\": ...}`) where Cobblemon's `Cube.uv` is `List<Int>?` - Cobblemon "
    "geo must be BOX UV.",
    f"{BLOCKBENCH}/TexturedModel.kt:297-304 (`from`, catch -> LOGGER.warn -> null); "
    f"{BLOCKBENCH}/TexturedModel.kt:339 `val uv: List<Int>? = null`; "
    f"{BLOCKBENCH}/repository/VaryingModelRepository.kt:697-700",
    "Re-export the model from Blockbench with Box UV enabled, or hand-convert the per-face "
    "`uv` objects back to a `[u, v]` pair.",
)

GEO_STRUCTURE_INVALID = _rule(
    "PL-F003",
    FATAL,
    "geo file structure breaks LayerDefinition creation",
    "`TexturedModel.create()` is called OUTSIDE the try/catch in `from()`, so a structural "
    "fault (missing `minecraft:geometry`, missing `description`, a bone whose `parent` is "
    "not defined earlier in the `bones` array) throws straight out of registerModels and "
    "no models load at all.",
    f"{BLOCKBENCH}/TexturedModel.kt:151-289 (createWithUvOverride; "
    "`parts[bone.parent]!!` at :182 requires the parent bone to appear EARLIER in the array); "
    f"{BLOCKBENCH}/repository/VaryingModelRepository.kt:701 `texturedModel.create().bakeRoot()`",
    "Fix the geo: one non-empty `minecraft:geometry` entry with a `description`, and order "
    "`bones` so every `parent` is declared before its children.",
)

RESOLVER_JSON_INVALID = _rule(
    "PL-F004",
    FATAL,
    "resolver / species JSON is unparseable",
    "`VaryingRenderableResolver.GSON.fromJson<ModelVariationSet>(json)` throws inside "
    "registerVariations with no try/catch, so variation loading aborts for the entire game.",
    f"{BLOCKBENCH}/repository/VaryingModelRepository.kt:586-594",
    "Fix the JSON syntax, or delete the file.",
)

POSER_JSON_INVALID = _rule(
    "PL-F005",
    FATAL,
    "poser JSON is unparseable",
    "`gson.fromJson(json, JsonObject::class.java)` in loadJsonPoser runs eagerly at asset "
    "load time and is not guarded, so registerJsonPosers throws and no posers load.",
    f"{BLOCKBENCH}/repository/VaryingModelRepository.kt:152-162 (loadJsonPoser) and "
    ":563-575 (registerJsonPosers)",
    "Fix the JSON syntax, or delete the file.",
)

UNKNOWN_POSE_TYPE = _rule(
    "PL-F006",
    FATAL,
    "poser declares an unknown poseType",
    "JsonPose throws `IllegalArgumentException: Unknown pose type X`. PoseAdapter rewraps it "
    "as `Failed to deserialize pose from JSON`, killing the render that touches this poser. "
    "Valid pose types are the PoseType enum constants (case-insensitive).",
    f"{BLOCKBENCH}/JsonPose.kt:58-61; entity/PoseType.kt:24-38",
    "Use one of: STAND WALK SLEEP HOVER FLY FLOAT SWIM GLIDE SHOULDER_LEFT SHOULDER_RIGHT "
    "PROFILE PORTRAIT OPEN NONE - or `\"allPoseTypes\": true`.",
)

ANIMATION_JSON_INVALID = _rule(
    "PL-F007",
    FATAL,
    "animation group JSON is unparseable",
    "BedrockAnimationRepository logs `Failed to load animation group` and drops the whole "
    "group, so every animation in the file is missing. Not fatal on its own, but if the file "
    "shadows a jar animation group by file stem (PL-R002) it silently deletes official "
    "animations, and any hard reference to them crashes (PL-C004).",
    f"{BLOCKBENCH}/bedrock/animation/BedrockAnimationRepository.kt:46-63",
    "Fix the JSON syntax, or delete the file.",
)


# --------------------------------------------------------------------------
# CRASH - loads fine, kills the client at render time.
# --------------------------------------------------------------------------

POSER_PART_MISSING = _rule(
    "PL-C001",
    CRASH,
    "JSON poser transformedParts[].part names a bone the paired model does not have",
    "`model.getPart(name)` is `relevantPartsByName[name]!!`. The JSON poser body is "
    "deserialized LAZILY (inside the lambda loadJsonPoser returns), so this survives asset "
    "load and detonates the first time the mon is rendered: "
    "`IllegalArgumentException: Failed to deserialize pose from JSON: {...}`. "
    "Available part names for a JSON poser are the DESCENDANTS of the resolved root bone "
    "plus the literal `__root` - the root bone's own name is NOT available.",
    f"{BLOCKBENCH}/JsonPose.kt:64-67 `model.getPart(partName).createTransformation()`; "
    f"{BLOCKBENCH}/PosableModel.kt:393 `fun getPart(name: String) = relevantPartsByName[name]!!`; "
    f"{BLOCKBENCH}/JsonModelAdapter.kt:31 `registerPartAndAllNamedChildren(\"__root\", rootBone)`; "
    f"{BLOCKBENCH}/PoseAdapter.kt:42-46 (rewrap); "
    f"{BLOCKBENCH}/repository/VaryingModelRepository.kt:155-161 (lazy deserialize)",
    "Rename the bone in the geo, or the `part` in the poser, so they agree. If the poser is a "
    "stale override of an official one, delete it (see --fix-manifest).",
)

BUILTIN_ROOT_BONE_MISSING = _rule(
    "PL-C002",
    CRASH,
    "built-in Kotlin poser's root bone is absent from the paired model",
    "`root.registerChildWithAllChildren(\"<name>\")` calls `ModelPart.getChild(name)!!`, which "
    "throws `java.util.NoSuchElementException: Can't find part <name>` from the model class "
    "constructor while rendering. The root bone must be a TOP-LEVEL geo bone (no `parent`).",
    f"{BLOCKBENCH}/PosableModel.kt:381-386 `val child = this.getChild(name)!!`; "
    f"{BLOCKBENCH}/repository/VaryingModelRepository.kt:577-579 (inbuilt) and "
    "client/render/VaryingRenderableResolver.kt:126 `poserSupplier(model)`",
    "Rename the geo's root bone to the name Cobblemon's model class expects, or move the "
    "variation onto a JSON poser you control.",
)

BUILTIN_PART_MISSING = _rule(
    "PL-C003",
    CRASH,
    "built-in Kotlin poser requires a bone the paired model does not have",
    "The model class calls `getPart(\"x\")` in its constructor (or `q.<fn>('x')` from a "
    "registered pose), which is `relevantPartsByName[\"x\"]!!` -> KotlinNullPointerException "
    "while rendering. For a built-in poser the available names are the root bone itself plus "
    "all of its descendants.",
    f"{BLOCKBENCH}/PosableModel.kt:393 + :404-411 (loadAllNamedChildren registers every "
    "descendant bone by name)",
    "Restore the missing bone in the geo, or stop overriding the official model for this "
    "species.",
)

BUILTIN_ANIMATION_MISSING = _rule(
    "PL-C004",
    CRASH,
    "built-in Kotlin poser references an animation that does not exist",
    "`PosableModel.bedrock(group, anim)` / `bedrockStateful(...)` resolve through "
    "`BedrockAnimationRepository.getAnimation`, which THROWS "
    "`IllegalArgumentException: Animation animation.<group>.<name> not found in animation "
    "group <group>` rather than returning null. This is the V36 Gliscor class of bug: our "
    "`cobblemon_reanimodel/.../gliscor.animation.json` shadowed the official group by file "
    "stem and was missing `cry`, so every Gliscor trade crashed both clients.",
    f"{BLOCKBENCH}/PosableModel.kt:824-836; "
    f"{BLOCKBENCH}/bedrock/animation/BedrockAnimationRepository.kt:76-82; "
    f"{BLOCKBENCH}/bedrock/animation/BedrockAnimationRepository.kt:57 "
    "(group key is the FILE STEM only - namespace and directory are discarded)",
    "Add the missing animation key to the shadowing group file, or delete the shadowing file "
    "so the official group loads again.",
)

POSER_LEGACY_ANIMATION_MISSING = _rule(
    "PL-C005",
    CRASH,
    "JSON poser uses the legacy `bedrock(group, anim)` form for a missing animation",
    "The legacy (non-MoLang) form goes through ANIMATION_FACTORIES -> "
    "BedrockAnimationReferenceFactory -> `model.bedrock(...)` -> `getAnimation` which THROWS. "
    "Unlike the modern `q.bedrock('g','a')` form (which is swallowed - see PL-W002), this one "
    "escapes JsonPose and PoseAdapter rewraps it as "
    "`Failed to deserialize pose from JSON`.",
    f"{BLOCKBENCH}/JsonPose.kt:112-124 (catch -> ANIMATION_FACTORIES path is NOT re-guarded); "
    f"{BLOCKBENCH}/BedrockAnimationReferenceFactory.kt:20-24",
    "Ship the animation, or switch the reference to the MoLang form `q.bedrock('g','a')` and "
    "accept a T-pose instead of a crash.",
)

POSER_QUIRK_UNRESOLVABLE = _rule(
    "PL-C006",
    CRASH,
    "JSON poser quirk expression cannot resolve",
    "String quirks are resolved eagerly and NOT wrapped in a try/catch: "
    "`json.asString.asExpressionLike().resolveObject(runtime).obj as SimpleQuirk`. An unknown "
    "MoLang function (or a cast failure) escapes JsonPose and PoseAdapter rewraps it as "
    "`Failed to deserialize pose from JSON`.",
    f"{BLOCKBENCH}/JsonPose.kt:135-138",
    "Use a known quirk function - `q.bedrock_quirk('group','anim')` or "
    "`q.bedrock_primary_quirk(...)`.",
)

RESOLVER_CASCADE = _rule(
    "PL-C007",
    CRASH,
    "species left uninitialised by an earlier resolver failure",
    "Cascade of PL-F001/PL-F002: because `variations.values.forEach { it.initialize(this) }` "
    "aborts on the first throwing resolver, every species iterated after it never gets its "
    "`repository` or `models` map. Rendering one of those crashes with "
    "`UninitializedPropertyAccessException` (repository) or a KotlinNullPointerException at "
    "`models[modelName]!!`. Map order is not stable, so which species survive is a lottery - "
    "this is why a single bad model reads as \"the whole pack is broken\".",
    "client/render/VaryingRenderableResolver.kt:41 (`lateinit var repository`), :119, :125 "
    "`val model = models[modelName]!!`; "
    f"{BLOCKBENCH}/repository/VaryingModelRepository.kt:602",
    "Fix the PL-F001/PL-F002 findings; this cascade disappears with them.",
)


# --------------------------------------------------------------------------
# WARN - renders, but wrong.
# --------------------------------------------------------------------------

POSER_MISSING = _rule(
    "PL-W001",
    WARN,
    "resolver variation.poser is neither a JSON poser nor a built-in poser",
    "`repository.posers[poserName]` is null, so getPoser throws IllegalStateException - which "
    "VaryingModelRepository.getPoser CATCHES and falls back to the `cobblemon:substitute` "
    "variation. The mon renders as a Substitute doll instead of itself.",
    f"{BLOCKBENCH}/repository/VaryingModelRepository.kt:633-643 (catch IllegalStateException "
    "-> fallback); client/render/VaryingRenderableResolver.kt:119",
    "Ship the poser JSON, or point the variation at a poser key that exists. Poser keys are "
    "`namespace:<filename minus .json>`.",
)

ANIMATION_MISSING = _rule(
    "PL-W002",
    WARN,
    "poser references an animation that does not exist (MoLang form)",
    "`q.bedrock('g','a')` / `q.bedrock_primary(...)` / `q.bedrock_stateful(...)` failures are "
    "caught by JsonPose's idle-animation try/catch, the factory lookup for `q.bedrock` misses "
    "(only the bare `bedrock` prefix is registered), and the animation is silently dropped - "
    "the mon T-poses in that pose. Note the animation KEY inside a group file is "
    "`animation.<group>.<name>`, and the group name is the FILE STEM with `.animation.json` "
    "removed.",
    f"{BLOCKBENCH}/JsonPose.kt:93-133; "
    f"{BLOCKBENCH}/PosableModel.kt:824-830 (`animationPrefix = \"animation.$animationGroup\"`); "
    f"{BLOCKBENCH}/bedrock/animation/BedrockAnimationRepository.kt:57",
    "Ship the animation under the right group file stem and the right "
    "`animation.<group>.<name>` key.",
)

TEXTURE_MISSING = _rule(
    "PL-W003",
    WARN,
    "resolver references a texture that does not exist",
    "Cobblemon does not validate textures at load; Minecraft renders the missing-texture "
    "checkerboard on the model.",
    "client/render/VaryingRenderableResolver.kt:174 (`texture: ModelTextureSupplier?`), "
    ":285-294 (ModelLayer.texture), :176 (`sprites`)",
    "Ship the .png, or fix the path in the resolver.",
)

DUPLICATE_KEY = _rule(
    "PL-W004",
    WARN,
    "two files in the pack collapse to the same loader key with different content",
    "Models, posers and animation groups are keyed by FILENAME, not by path. Two files with "
    "the same basename in different scanned directories (e.g. `bedrock/models/x.geo.json` and "
    "`bedrock/pokemon/models/x.geo.json`) overwrite each other; which one wins depends on the "
    "directory order in VaryingModelRepository, and for two files in the SAME directory it is "
    "undefined resource-manager order.",
    f"{BLOCKBENCH}/repository/VaryingModelRepository.kt:100-106 (modelDirectories / "
    "animationDirectories order - LATER directory wins), :607-622 (registerModels), "
    ":563-575 (registerJsonPosers)",
    "Delete the loser, or rename one of the two files.",
)

JUNK_FILE = _rule(
    "PL-W005",
    WARN,
    "junk file shipped inside the pack",
    "Editor/OS residue (.DS_Store, Thumbs.db, *.bak, *.orig, __MACOSX) bloats the pack and "
    "confuses diffs. `*.bak` copies of a .json in a scanned directory are worse than dead "
    "weight: a `foo.geo.json.bak` is ignored, but a `foo.bak.json` IS loaded and can win a "
    "key collision.",
    f"{BLOCKBENCH}/repository/VaryingModelRepository.kt:566 / :611 "
    "(`path.endsWith(\".json\")` / `.endsWith(\".geo.json\")` - the only filter applied)",
    "Delete them (see --fix-manifest).",
)

DATA_IN_RESOURCE_PACK = _rule(
    "PL-W006",
    WARN,
    "data/ entries inside a client resource pack",
    "A client resource pack's `data/` tree is dead weight - the client never reads it and "
    "PackSquash strips it anyway. Its presence usually means a datapack was merged into a "
    "resource pack by mistake, which is how species changes get shipped where nothing reads "
    "them.",
    "DEPLOY.md: 'Datapack deploys are SEPARATE from resource-pack deploys'; PackSquash step "
    "drops data/.",
    "Move the content to the datapack and delete it from the resource pack.",
)

POSER_ANIM_BONE_MISSING = _rule(
    "PL-W007",
    WARN,
    "poser animation references a bone the paired model does not have",
    "`q.look('head')`, `q.quadruped_walk(...)`, `q.biped_walk(...)`, `q.bimanual_swing(...)`, "
    "`q.sine_wing_flap(...)`, `q.punch(...)` and `q.pitch_tilt(...)` all call "
    "`model.getPart(name)` (`!!`). Inside a pose's `animations` array that NPE is caught by "
    "JsonPose and the animation is silently dropped - the mon renders but does not move that "
    "bone.",
    "client/ClientMoLangFunctions.kt:130-253 (each `model.getPart(...)`); "
    f"{BLOCKBENCH}/JsonPose.kt:112-125 (catch -> null -> animation dropped)",
    "Name the bone as the animation expects, or pass the real bone name as the function "
    "argument.",
)

INERT_PACK_ASSET = _rule(
    "PL-W009",
    WARN,
    "pack file is inert - another file wins its loader key",
    "Models, posers and animation groups are keyed by FILENAME only, so a pack file whose key "
    "is claimed by a file in a later-scanned directory (or by another file in the same "
    "directory) never loads. Shipping it does nothing, and - worse - editing it to fix a bug "
    "does nothing either, which is how a 'fixed' pack keeps reoffending.",
    f"{BLOCKBENCH}/repository/VaryingModelRepository.kt:100-106 (modelDirectories order), "
    ":607-622 (`texturedModels[it.first] = it.second` - last write wins), :563-575; "
    f"{BLOCKBENCH}/bedrock/animation/BedrockAnimationRepository.kt:57-58",
    "Delete the inert file, or rename the winner/loser so the key you intended actually loads.",
)

ROOT_BONE_UNRESOLVABLE = _rule(
    "PL-W008",
    WARN,
    "JSON poser cannot resolve a root bone from the paired model",
    "loadJsonPoser picks `rootBone` -> the top-level bone named after the poser file -> "
    "`children.entries.filter { LocatorAccess.PREFIX !in it.key }.first()`. If the model has "
    "no non-locator top-level bone that `.first()` throws NoSuchElementException. If it falls "
    "through to an arbitrary first bone, the poser silently poses the wrong subtree.",
    f"{BLOCKBENCH}/repository/VaryingModelRepository.kt:156-157",
    "Add an explicit `\"rootBone\": \"<bone>\"` to the poser, or name the geo's root bone after "
    "the poser file.",
)


# --------------------------------------------------------------------------
# RESIDUE - the class SPG hates: our overrides of assets the jar now ships.
# --------------------------------------------------------------------------

STALE_PATH_OVERRIDE = _rule(
    "PL-R001",
    RESIDUE,
    "pack file replaces an official jar file at the same path",
    "The pack wins, so players get our copy of an asset Cobblemon now maintains itself. "
    "Every Cobblemon release that adds or reworks a species turns another one of these from "
    "'our content' into 'a stale fork of theirs'.",
    "Resource pack precedence: a pack entry at the same `assets/<ns>/<path>` shadows the jar "
    "entry for every scanned directory in "
    f"{BLOCKBENCH}/repository/VaryingModelRepository.kt:85-106",
    "Delete the pack copy unless the operator config marks it Eclipse-owned.",
)

STALE_KEY_OVERRIDE = _rule(
    "PL-R002",
    RESIDUE,
    "pack file shadows an official asset by loader key from a different path",
    "The sneaky one. Models, posers and animation groups are keyed by filename only, so "
    "`assets/cobblemon_reanimodel/bedrock/pokemon/animations/gliscor/gliscor.animation.json` "
    "replaces the jar's `.../0472_gliscor/gliscor.animation.json` even though the paths differ "
    "and even though the namespace differs (animation group keys have NO namespace at all). "
    "This is exactly how V36 shipped a Gliscor with no `cry` and crashed both clients on every "
    "trade.",
    f"{BLOCKBENCH}/repository/VaryingModelRepository.kt:570 and :694 "
    "(`ResourceLocation.fromNamespaceAndPath(identifier.namespace, "
    "File(identifier.path).nameWithoutExtension)`); "
    f"{BLOCKBENCH}/bedrock/animation/BedrockAnimationRepository.kt:57 "
    "(group key = file stem, namespace discarded)",
    "Delete the pack copy, or - if it is deliberately Eclipse-owned - rename it to a key that "
    "cannot collide and mark the namespace/aspect in eclipse-owned.json.",
)

ANIMATION_GROUP_KEYS_LOST = _rule(
    "PL-R003",
    RESIDUE,
    "shadowing animation group drops keys the official group had",
    "A pack animation group that wins the file-stem key but defines fewer animations than the "
    "official one silently deletes those animations for every model that references them. "
    "Combined with PL-C004 (hard reference -> throw) this is a client crash; on its own it is "
    "a T-pose.",
    f"{BLOCKBENCH}/bedrock/animation/BedrockAnimationRepository.kt:44-59 "
    "(whole group is replaced, never merged)",
    "Copy the missing `animation.<group>.<name>` keys from the jar's group into the pack's, or "
    "delete the pack's copy.",
)


# --------------------------------------------------------------------------


@dataclass
class Finding:
    rule: str
    severity: str
    #: Short one-line summary shown in the report.
    message: str
    #: Pack-relative path (or jar path) the finding is about, when there is one.
    path: str | None = None
    #: The source zip/dir the file came from.
    source: str | None = None
    #: Free-form structured detail for the JSON report.
    detail: dict[str, Any] = field(default_factory=dict)
    #: Paths a --fix-manifest should propose deleting.
    suggest_delete: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
        }
        if self.path:
            out["path"] = self.path
        if self.source:
            out["source"] = self.source
        if self.detail:
            out["detail"] = self.detail
        if self.suggest_delete:
            out["suggest_delete"] = self.suggest_delete
        return out


def make(rule: Rule, message: str, **kwargs: Any) -> Finding:
    return Finding(rule=rule.id, severity=rule.severity, message=message, **kwargs)
