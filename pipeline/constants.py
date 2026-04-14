"""Shared constants for the bird observatory pipeline."""

# Camera names (used in classifier config, health reporting, SSE events)
CAMERA_FEEDER = "feeder"
CAMERA_GROUND = "ground"


# Model source labels (used in classifier results, SSE events, DB)
class ModelSource:
    YARD = "yard"
    AIY = "aiy"
    BOTH_AGREE = "both_agree"
    VOTE_PLURALITY = "vote_plurality"
