#!/usr/bin/env python3
"""Burn a machine-readable timecode strip into the demo video.

Strip (rows 0-9, native 640x360): 26 blocks of 8x8px, 1 bit each:
  block 0 = WHITE, block 1 = BLACK  (sync/polarity markers)
  blocks 2-17  = 16-bit frame index, MSB first
  blocks 18-25 = 8-bit checksum (XOR of the two index bytes)
Pure black/white 8x8 blocks survive H.264; decode = sample each block center.
Also a human-readable "f NNNNN" top-right. Verifies decode-back from the
ENCODED file on 200 sampled frames before declaring success.
"""
import os
import sys
import tempfile
import av
import cv2
import numpy as np

# Usage: make_timecode_demo.py [SRC] [OUT]
#   SRC defaults to the sim reel; OUT defaults to SRC (safe in-place: we encode
#   to a temp file in the same directory, verify decode-back, then atomically
#   replace — the input is never truncated mid-read).
SRC = sys.argv[1] if len(sys.argv) > 1 else "/home/vives/sim/current.mp4"
OUT = sys.argv[2] if len(sys.argv) > 2 else SRC
_inplace = os.path.abspath(SRC) == os.path.abspath(OUT)
_tmp = tempfile.NamedTemporaryFile(
    dir=os.path.dirname(os.path.abspath(OUT)) or ".",
    suffix=".mp4", delete=False).name
_write_to = _tmp if _inplace else OUT

BLOCK = 8
NBLOCKS = 26

def stamp(img, idx):
    hi, lo = (idx >> 8) & 0xFF, idx & 0xFF
    chk = hi ^ lo
    bits = [1, 0]
    bits += [(idx >> (15 - i)) & 1 for i in range(16)]
    bits += [(chk >> (7 - i)) & 1 for i in range(8)]
    img[0:BLOCK + 2, 0:NBLOCKS * BLOCK + 2] = 0        # black bed behind blocks
    for i, b in enumerate(bits):
        v = 255 if b else 0
        img[1:BLOCK + 1, 1 + i * BLOCK: 1 + (i + 1) * BLOCK] = v
    cv2.putText(img, f"f {idx:05d}", (640 - 92, 9), cv2.FONT_HERSHEY_SIMPLEX,
                0.35, (255, 255, 255), 1, cv2.LINE_AA)
    return img

def decode_strip(img):
    bits = []
    for i in range(NBLOCKS):
        px = img[1 + BLOCK // 2, 1 + i * BLOCK + BLOCK // 2]
        v = int(px if np.isscalar(px) else px.mean())
        bits.append(1 if v > 127 else 0)
    if bits[0] != 1 or bits[1] != 0:
        return None
    idx = 0
    for b in bits[2:18]:
        idx = (idx << 1) | b
    chk = 0
    for b in bits[18:26]:
        chk = (chk << 1) | b
    if chk != (((idx >> 8) & 0xFF) ^ (idx & 0xFF)):
        return None
    return idx

# ---- encode ----
inc = av.open(SRC)
vs = inc.streams.video[0]
outc = av.open(_write_to, "w")
ovs = outc.add_stream("h264", rate=30)
ovs.width, ovs.height = 640, 360
ovs.pix_fmt = "yuv420p"
ovs.options = {"crf": "20", "preset": "veryfast"}

n = 0
for frame in inc.decode(vs):
    img = frame.to_ndarray(format="bgr24")
    img = np.ascontiguousarray(img)
    stamp(img, n)
    ofr = av.VideoFrame.from_ndarray(img, format="bgr24")
    for pkt in ovs.encode(ofr):
        outc.mux(pkt)
    n += 1
for pkt in ovs.encode():
    outc.mux(pkt)
outc.close(); inc.close()
print(f"encoded {n} stamped frames -> {_write_to}")

# ---- verify decode-back from the ENCODED file ----
inc = av.open(_write_to)
vs = inc.streams.video[0]
ok = bad = 0
expected = 0
for frame in inc.decode(vs):
    if expected % 23 == 0 or expected < 5 or expected > n - 5:  # ~200 samples
        img = frame.to_ndarray(format="bgr24")
        got = decode_strip(img)
        if got == expected: ok += 1
        else: bad += 1
        if bad and bad <= 3:
            print(f"  MISMATCH at frame {expected}: decoded {got}")
    expected += 1
inc.close()
print(f"decode-back: {ok} ok, {bad} bad")
success = bad == 0 and ok > 150
if _inplace:
    if success:
        os.replace(_tmp, OUT)      # atomic: only replace after verification
        print(f"in-place replace verified -> {OUT}")
    else:
        os.unlink(_tmp)
        print("verification FAILED — original left untouched")
sys.exit(0 if success else 1)
