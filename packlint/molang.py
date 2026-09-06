"""Extract the bits of a MoLang animation string that we can check statically.

We do not evaluate MoLang. We only need two things out of a pose's animation /
quirk strings:

* which `(animation group, animation name)` pairs it will ask
  BedrockAnimationRepository for, and
* which bone names it will pass to `model.getPart(...)`.

Both are string literals in practice, so a regex over the call is enough and
degrades safely: an expression we cannot read produces no claims, never a false
finding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_STR = r"""['"]([^'"]*)['"]"""

#: q.bedrock('group', 'anim') and friends -> BedrockAnimationRepository lookups.
#: ClientMoLangFunctions.kt:71 / :109 / :121, PosableModel.kt:824-836
_ANIM_FUNCS = ("bedrock", "bedrock_primary", "bedrock_stateful")
#: Quirk forms take a group plus one animation OR an array of animations.
#: ClientMoLangFunctions.kt:253 / :325
_QUIRK_FUNCS = ("bedrock_quirk", "bedrock_primary_quirk")

#: MoLang functions that resolve a bone through `model.getPart(name)` (a `!!`).
#: ClientMoLangFunctions.kt:130-253. Value is (arg index -> default bone name).
_BONE_FUNCS: dict[str, dict[int, str | None]] = {
    "look": {0: None},
    "pitch_tilt": {0: "root"},
    "quadruped_walk": {
        2: "leg_front_left",
        3: "leg_front_right",
        4: "leg_back_left",
        5: "leg_back_right",
    },
    "biped_walk": {2: "leg_left", 3: "leg_right"},
    "bimanual_swing": {2: "arm_left", 3: "arm_right"},
    "sine_wing_flap": {4: "wing_left", 5: "wing_right"},
    "punch": {0: "head", 1: "body", 2: "arm_left", 3: "arm_right"},
}

#: The legacy, non-MoLang animation form. JsonPose.kt:112-124 routes this
#: through ANIMATION_FACTORIES, where a miss THROWS instead of being swallowed.
_LEGACY_ANIM = re.compile(r"^\s*bedrock\s*\(([^)]*)\)\s*$")

#: Every MoLang function the pose pipeline knows about, for PL-C006. A quirk
#: string naming anything else cannot resolve, and quirk resolution is not
#: guarded (JsonPose.kt:135-138).
KNOWN_QUIRK_FUNCS = frozenset(_QUIRK_FUNCS)


@dataclass(frozen=True)
class AnimRef:
    group: str
    name: str
    #: True when a miss throws rather than being swallowed.
    hard: bool

    @property
    def key(self) -> str:
        """The key inside the group file: `animation.<group>.<name>`."""
        return f"animation.{self.group}.{self.name}"


def _calls(text: str, func: str) -> list[list[str]]:
    """Return the raw argument lists of every `q.<func>(...)` / `<func>(...)`."""
    out: list[list[str]] = []
    pattern = re.compile(r"(?:\b(?:q|query|v|variable)\.)?" + re.escape(func) + r"\s*\(")
    for match in pattern.finditer(text):
        # Reject a longer function name that merely ends with ours
        # (`bedrock_primary_quirk` vs `bedrock_quirk`).
        start = match.start()
        if start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
            continue
        depth = 1
        i = match.end()
        while i < len(text) and depth:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        out.append(_split_args(text[match.end() : i - 1]))
    return out


def _split_args(body: str) -> list[str]:
    args: list[str] = []
    depth = 0
    current = ""
    quote = ""
    for ch in body:
        if quote:
            current += ch
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            current += ch
        elif ch in "([":
            depth += 1
            current += ch
        elif ch in ")]":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            args.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        args.append(current.strip())
    return args


def _literal(arg: str) -> str | None:
    m = re.fullmatch(_STR, arg.strip())
    return m.group(1) if m else None


def animation_refs(text: str) -> list[AnimRef]:
    """Animation lookups a pose animation / quirk string will perform."""
    refs: list[AnimRef] = []

    legacy = _LEGACY_ANIM.match(text)
    if legacy and "q." not in text and "query." not in text:
        args = _split_args(legacy.group(1))
        if len(args) >= 2:
            group = _literal(args[0]) or args[0].strip()
            name = _literal(args[1]) or args[1].strip()
            if group and name:
                refs.append(AnimRef(group, name, hard=True))
        return refs

    for func in _ANIM_FUNCS:
        for args in _calls(text, func):
            if len(args) < 2:
                continue
            group, name = _literal(args[0]), _literal(args[1])
            if group and name:
                refs.append(AnimRef(group, name, hard=False))

    for func in _QUIRK_FUNCS:
        for args in _calls(text, func):
            if len(args) < 2:
                continue
            group = _literal(args[0])
            if not group:
                continue
            # arg 1 is one name or an array of names.
            names = [_literal(a) for a in _split_args(args[1].strip("[]"))]
            for name in names:
                if name:
                    refs.append(AnimRef(group, name, hard=False))

    return refs


def bone_refs(text: str) -> list[str]:
    """Bone names a pose animation string will pass to `model.getPart(...)`."""
    bones: list[str] = []
    for func, slots in _BONE_FUNCS.items():
        for args in _calls(text, func):
            for index, default in slots.items():
                if index < len(args):
                    literal = _literal(args[index])
                    if literal:
                        bones.append(literal)
                elif default is not None:
                    bones.append(default)
    return bones


_TOP_LEVEL_CALL = re.compile(r"^\s*(?:q|query|v|variable)\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def top_level_function(text: str) -> str | None:
    """The OUTERMOST MoLang function of an expression, or None if it is not
    a single call.

    Only the outer call decides what a string-form quirk resolves to; the
    arguments are routinely other functions (`q.array(...)`, `q.curve(...)`)
    and reading those as the quirk produced 121 phantom PL-C006s.
    """
    match = _TOP_LEVEL_CALL.match(text)
    if not match:
        return None
    depth = 0
    for index, ch in enumerate(text[match.end() - 1 :], start=match.end() - 1):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                # A trailing operator means the whole string is an expression,
                # not one call - we cannot say what it resolves to, so we do not
                # claim anything.
                return match.group(1) if not text[index + 1 :].strip() else None
    return None
