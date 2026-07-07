# Re-export the LiteRT interpreter under the legacy tflite_runtime name.
# See package __init__ for why. API-compatible: ai-edge-litert is the
# continuation of the tflite-runtime project.
from ai_edge_litert.interpreter import *          # noqa: F401,F403
from ai_edge_litert.interpreter import Interpreter  # noqa: F401
