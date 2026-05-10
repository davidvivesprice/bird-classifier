#!/usr/bin/env python3
"""C1 verification: does PyAV's mpegts muxer preserve packet PTS verbatim?

The bedrock spec assumes that demuxing packets from the camera RTSP and
writing them through PyAV's mpegts muxer (no decode, no re-encode) preserves
PTS values byte-exact. The reviewer flagged this as unverified.

This script:
  1. Opens an RTSP source (or local file) via PyAV
  2. Demuxes ~30 video packets, recording their PTS values
  3. Writes those same packets to /tmp/test_seg.ts via mpegts muxer
  4. Re-opens /tmp/test_seg.ts via PyAV
  5. Demuxes again, recording the PTS values from the muxed output
  6. Reports diff: input_pts[i] vs output_pts[i]

Pass: |input_pts[i] - output_pts[i]| <= 1ms equivalent (90 ticks at 90kHz)
      for all i. Means PTS is preserved across the muxer.
Fail: any drift, especially monotonic rebase to 0.

Usage:
    python3 prototype_hls_passthrough.py <input_url>
    e.g. rtsp://127.0.0.1:8554/feeder-main
"""
import sys
import os
import av
import tempfile

INPUT = sys.argv[1] if len(sys.argv) > 1 else "rtsp://127.0.0.1:8554/feeder-main"
OUTPUT = "/tmp/test_seg.ts"
N_PACKETS = 30  # ~1 second at 30fps


def demux_pts(input_url, n=N_PACKETS, options=None):
    """Open url, demux n video packets, return list of (pts, dts, is_key, size)."""
    if options is None:
        options = {}
    if input_url.startswith("rtsp://"):
        options.setdefault("rtsp_transport", "tcp")
        options.setdefault("fflags", "nobuffer")
    container = av.open(input_url, options=options)
    try:
        vs = container.streams.video[0]
        results = []
        for packet in container.demux(vs):
            if packet.pts is None:
                continue
            results.append({
                "pts": packet.pts,
                "dts": packet.dts,
                "is_key": packet.is_keyframe,
                "size": packet.size,
                "time_base": vs.time_base,
            })
            if len(results) >= n:
                break
        return results, vs
    finally:
        container.close()


def write_ts(input_url, output_path, n=N_PACKETS):
    """Open url, demux n packets, write to output_path as mpegts. Returns
    list of input packets (their PTS, etc.) so we can diff against output."""
    in_container = av.open(input_url, options={
        "rtsp_transport": "tcp",
        "fflags": "nobuffer",
    } if input_url.startswith("rtsp://") else {})
    in_stream = in_container.streams.video[0]
    out_container = av.open(output_path, mode="w", format="mpegts")
    out_stream = out_container.add_stream_from_template(in_stream)

    input_pkts = []
    written = 0
    started = False
    for packet in in_container.demux(in_stream):
        if packet.pts is None:
            continue
        # Wait for a keyframe to start (mpegts requires a keyframe at SOI)
        if not started:
            if not packet.is_keyframe:
                continue
            started = True
        input_pkts.append({
            "pts": packet.pts,
            "dts": packet.dts,
            "is_key": packet.is_keyframe,
            "size": packet.size,
            "time_base": in_stream.time_base,
        })
        # Re-stream the packet
        packet.stream = out_stream
        out_container.mux(packet)
        written += 1
        if written >= n:
            break

    out_container.close()
    in_container.close()
    return input_pkts


def main():
    print(f"Input: {INPUT}")
    print(f"Output segment: {OUTPUT}")
    print()

    print(f"=== Phase 1: write {N_PACKETS} packets to {OUTPUT} ===")
    input_pkts = write_ts(INPUT, OUTPUT, n=N_PACKETS)
    print(f"Wrote {len(input_pkts)} packets to {OUTPUT}")
    print(f"File size: {os.path.getsize(OUTPUT)} bytes")
    print(f"First input PTS: {input_pkts[0]['pts']} ({float(input_pkts[0]['pts'] * input_pkts[0]['time_base']):.3f}s)")
    print(f"Last  input PTS: {input_pkts[-1]['pts']} ({float(input_pkts[-1]['pts'] * input_pkts[-1]['time_base']):.3f}s)")
    print(f"Time base: {input_pkts[0]['time_base']}")
    print()

    print(f"=== Phase 2: re-demux {OUTPUT} and compare PTS ===")
    output_pkts, out_stream = demux_pts(OUTPUT, n=N_PACKETS + 5)
    print(f"Read back {len(output_pkts)} packets from {OUTPUT}")
    print(f"First output PTS: {output_pkts[0]['pts']} ({float(output_pkts[0]['pts'] * output_pkts[0]['time_base']):.3f}s)")
    print(f"Last  output PTS: {output_pkts[-1]['pts']} ({float(output_pkts[-1]['pts'] * output_pkts[-1]['time_base']):.3f}s)")
    print(f"Output time base: {output_pkts[0]['time_base']}")
    print()

    # Time bases may differ between input and output; compare PTS in seconds.
    print(f"=== Phase 3: per-packet PTS comparison (in seconds) ===")
    print(f"{'i':>3} {'input_pts':>14} {'input_s':>12} {'output_pts':>14} {'output_s':>12} {'diff_ms':>10} {'key':>4}")

    n_compare = min(len(input_pkts), len(output_pkts))
    max_diff_ms = 0.0
    diffs = []
    for i in range(n_compare):
        in_s = float(input_pkts[i]["pts"] * input_pkts[i]["time_base"])
        out_s = float(output_pkts[i]["pts"] * output_pkts[i]["time_base"])
        diff_ms = (out_s - in_s) * 1000.0
        diffs.append(diff_ms)
        max_diff_ms = max(max_diff_ms, abs(diff_ms))
        print(f"{i:>3} {input_pkts[i]['pts']:>14} {in_s:>12.3f} {output_pkts[i]['pts']:>14} {out_s:>12.3f} {diff_ms:>+10.3f} {'K' if input_pkts[i]['is_key'] else '.':>4}")

    print()
    print(f"=== Verdict ===")
    print(f"Max |output_s - input_s| across {n_compare} packets: {max_diff_ms:.3f} ms")
    if max_diff_ms <= 1.0:
        print("PASS: PTS preserved within 1ms tolerance. C1 ASSUMPTION HOLDS.")
        print(f"  Conclusion: passthrough mux preserves PTS. Spec's design is sound on this axis.")
        return 0
    elif all(abs(d - diffs[0]) < 1.0 for d in diffs):
        # Constant offset — muxer rebased to start at 0 (or other constant)
        print(f"FAIL: PTS rebased by constant {diffs[0]:.3f} ms.")
        print(f"  Conclusion: muxer rebases PTS. Sidecar can record the offset, OR we set time_base/start_time explicitly. Need a fix.")
        return 1
    else:
        print(f"FAIL: PTS drift is non-constant. Max delta {max_diff_ms:.3f} ms.")
        print(f"  Conclusion: serious issue. Different fix needed.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
