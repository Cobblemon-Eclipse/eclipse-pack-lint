"""eclipse-pack-lint - static validator for Cobblemon client resource packs.

Emulates Cobblemon's client-side asset loader (VaryingModelRepository,
VaryingRenderableResolver, JsonPose/PoseAdapter, BedrockAnimationRepository)
against a jar + pack overlay, so that pack faults that would only surface as a
client crash are caught before a pack ever reaches players.
"""

__version__ = "1.0.0"
