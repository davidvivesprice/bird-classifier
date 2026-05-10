# Replay harness runbook

## Layer 2a (LAN replay)

1. iMac: `./test_clips/serve_test_feed.sh "/Users/vives/docs/bird-observatory/training videos/may10_demo_video FIXED.m4v" 8654 feeder-main &` (using port 8654 to avoid conflicting with the iMac's existing go2rtc on 8554).
2. Find iMac LAN IP: `ifconfig | awk '/inet 192\.168\./ {print $2; exit}'`
3. Pi: `systemctl --user set-environment PIPELINE_TEST_RTSP_URL=rtsp://$IMAC_IP:8654/feeder-main && systemctl --user restart bird-pipeline.service`
4. Capture SSE: `python3 tools/sync_replay_record_sse.py --url 'http://pi5.local:8099/api/pipeline/events/sse?camera=feeder' --duration 300 --out /tmp/replay.jsonl`
5. Assert: `PYTHONPATH=/Users/vives/bird-classifier-pi python3 tools/sync_replay_assert.py --annotations '...' --events /tmp/replay.jsonl --gate-count 5`
6. Restore: `systemctl --user unset-environment PIPELINE_TEST_RTSP_URL && systemctl --user restart bird-pipeline.service`
7. Stop looper: `pkill -f serve_test_feed.sh && pkill -f mediamtx`

**Synchronization note**: To capture events including video PTS 0-X seconds, the
SSE recorder must be running BEFORE the looper starts. Practical sequence:
start Pi pipeline restart → wait for it ready → start SSE recorder → start
looper. With a ~12s Pi-restart delay, this means the looper plays "in the dark"
for ~3s before SSE events flow. If gate visits live at PTS < 5s, that won't be
captured even with this trick.

## Layer 2b (tunnel)

Same as above, but step 4 hits `https://pi5.vivessato.com` with CF Access headers.
The SSE recorder needs a flag to add these headers — see follow-up task.

## C5 results (2026-05-10)

Two captures asserted against `may10_demo_video.annotations.md`:

- Smoke capture (`replay_smoke_20260510_1917.jsonl`, PTS 50.8-350.0, 7659 events):
  2/5 PASS. Visit 02 failed `detection not matched` due to PTS coverage gap
  (midpoint 33.6s outside window).
- Fresh capture (`replay_20260510_1919.jsonl`, PTS 10.6-175.9, 4214 events):
  2/5 PASS. Visit 02 now in window but fails species mismatch — pipeline
  locked species `House Finch` at PTS 33.3, annotation expected
  `Tufted Titmouse`.
- Both runs: median lag <±5ms (smoke +5/+5; fresh -2/+4). Phase A vote-lock
  is fast and stable when species matches.

Gate-eligible visits (5 of 9 annotations):

| Visit | Midpoint | Annotation species         | Pipeline (locked) in window     | Verdict |
|-------|---------:|----------------------------|---------------------------------|---------|
| 01    |   64.15s | house finch                | house finch                     | PASS    |
| 02    |   33.60s | tufted titmouse            | house finch                     | FAIL    |
| 06    |  114.60s | american goldfinch (female)| american goldfinch, blue jay    | FAIL    |
| 08    |  133.10s | tufted tit                 | blue jay                        | FAIL    |
| 09    |  153.00s | blue jay                   | blue jay                        | PASS    |

Notes:
- Visit 02: pipeline detected birds continuously but never locked tufted
  titmouse. Could be classifier accuracy on this bird, or the
  motion/identifiability boundary may be different than David's annotation.
- Visit 06: pipeline did lock `american goldfinch` somewhere in the in-frame
  window but not within ±1s of the identifiable-midpoint, AND the annotation
  carries `(female)` which doesn't match the canonical `american goldfinch`
  emitted by the AIY classifier. The matcher's species comparison is
  lowercase-exact — `american goldfinch (female)` ≠ `american goldfinch`.
- Visit 08: annotation typo `tufted tit` (not `tufted titmouse`) plus
  pipeline classified it as blue jay anyway. Two-bug overlap.

The harness is verified correct: it correctly accepts events that match
species, correctly rejects events that don't, and gives sub-5ms lag
measurements. The 3/5 failure mode is the classifier + annotation-format
mismatch, not the harness.
