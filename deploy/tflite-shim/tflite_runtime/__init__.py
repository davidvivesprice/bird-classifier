# tflite_runtime -> ai_edge_litert compatibility shim (Pi port 2026-07-07).
#
# birdnetlib imports `tflite_runtime.interpreter` (falling back to full
# tensorflow, which we don't want on the Pi). Google discontinued
# tflite-runtime wheels after Python 3.11 — its official successor is
# ai-edge-litert, which ships aarch64/py3.13 wheels and preserves the
# Interpreter API verbatim. This two-file package makes the old import name
# resolve to the new runtime.
#
# Install: copy this `tflite_runtime/` directory into the venv's
# site-packages (deploy step in deploy/systemd/bird-audio.service header).
