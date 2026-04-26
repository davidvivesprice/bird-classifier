# 08 · Deployment & Workflow

How code gets from edits on the iMac to running on the Pi, plus the cold-start runbook for bringing a fresh Pi up to live.

## Repo split (the post-2026-04-25 reality)

After 2026-04-25 the iMac and Pi sides have **separate repos with separate git histories**. They share linear history through commit `5773551` and diverge from there. Cross-cutting fixes flow as `[patch]` posts in the comms file, NOT silent shared-file edits.

| | Path | Who edits it |
|---|---|---|
| iMac-side | `/Users/vives/bird-classifier/` | iMac-Claude only |
| Pi-side, on iMac | `/Users/vives/bird-classifier-pi/` | Pi-Claude — this is where Pi commits land |
| Pi-side, runtime | `vives@pi5.local:/home/vives/bird-classifier/` | rsync target. Where the services run. NOT a git source of truth post-split. |

See `working/progress/2026-04-25-pi-repo-split.md` for the full split context.

## The edit-and-deploy loop

```
edit on iMac at /Users/vives/bird-classifier-pi/
        │
        ▼
git commit (Pi-side history)
        │
        ▼
rsync to vives@pi5.local:/home/vives/bird-classifier/
        │
        ▼
systemctl --user restart <service>  (if needed)
```

Concretely:

```bash
# After editing, e.g., dashboard/pi_dash.html:
rsync -av /Users/vives/bird-classifier-pi/dashboard/pi_dash.html \
    vives@pi5.local:/home/vives/bird-classifier/dashboard/
ssh vives@pi5.local "systemctl --user restart bird-dashboard"
```

For Pi-only modules (anything in `pipeline/hailo_*.py`, `pi_classifier.py`, `model_registry.py`, `dashboard/pi_dash.html`, `dashboard/pi_review.py`, `tools/pi5_thermal_watch.py`): no PI_MODE gating needed — they're imported only when `PI_MODE=1`. Edit freely.

For shared files (`bird_pipeline_v3.py`, `dashboard/api.py`, `pipeline/frame_capture.py`, `pipeline/hires_ring.py`): edits inside the Pi-side repo are fine — the iMac repo has its own copy and won't see the changes. If the change is a strict bug fix that benefits both sides, post a `[patch]` to `working/progress/cross-claude-comms.md` so iMac-Claude can apply on their side.

## Pi runtime layout

`/home/vives/bird-classifier/` mirrors the Pi-side repo. The Pi's `.git/` exists (from the original cp -a) but is no longer authoritative — don't commit there.

`~/.config/systemd/user/` holds the four service unit files plus the thermal-watch timer + service. See `02-services.md`.

`~/.bird-observatory-env` holds pipeline env vars (UNIFI_API_KEY, PI_CLASSIFIER, PIPELINE_HIRES_RING). See `02-services.md`.

`~/bird-snapshots/` holds runtime data (classified JPGs, annotated copies, HLS segments, SQLite DBs, logs). See `01-hardware.md`.

## Cold-start runbook (cheat sheet)

This is a condensed version of `~/docs/bird-observatory/historical/35-pi5-prep-runbook.md` — read that for the full story when the Pi is fresh out of a flash.

```bash
# 1. Confirm services up
ssh vives@pi5.local "systemctl --user is-active bird-pipeline bird-dashboard go2rtc cloudflared"

# 2. Confirm pipeline is processing
ssh vives@pi5.local "curl -sS http://localhost:8100/api/pipeline/health | python3 -m json.tool"

# 3. Latest classifications (visible in the dashboard's Recent strip)
ssh vives@pi5.local "sqlite3 -column -header ~/bird-snapshots/logs/classifications.db \\
  \"SELECT source_timestamp, common_name, ROUND(confidence,3) AS conf \\
   FROM classifications WHERE action='classified' ORDER BY id DESC LIMIT 8\""

# 4. Latest snapshot resolution (should be 1920x1080 with PIPELINE_HIRES_RING=authoritative)
ssh vives@pi5.local "ls -t ~/bird-snapshots/classified/*/*.jpg | head -1 | \\
  xargs -I {} ~/bird-classifier/venv/bin/python3 -c \\
  \"import sys; from PIL import Image; im=Image.open(sys.argv[1]); print(im.size)\" {}"

# 5. Thermal + fan
ssh vives@pi5.local "echo -n 'temp: '; awk '{printf \"%.1fC\\n\", \$1/1000}' \\
  /sys/class/thermal/thermal_zone0/temp; \\
  echo -n 'fan: '; cat /sys/class/hwmon/*/fan1_input 2>/dev/null"

# 6. Hailo identify
ssh vives@pi5.local "hailortcli fw-control identify"

# 7. Pi log tail
ssh vives@pi5.local "tail -50 ~/logs/bird-pipeline.log"
```

## Coordination with iMac-Claude

Cross-platform notes go in `working/progress/cross-claude-comms.md` — append-only, never edit prior entries. David relays nudges between the two Claudes; we can't poll each other in real-time. Subject prefixes lets readers scan threads:

- `[hello]` — first contact
- `[patch]` — cross-cutting fix that the other side should apply
- `[hailo-multimodel]` / `[snapshot-arch]` / `[review-system]` — topic threads
- `[fyi]` — heads-up only

Each entry has a small header (sender → recipient, timestamp, "Needs response: yes/no/fyi", subject) — see the file's preamble for the protocol.

## Don't-do list

- **Don't** push to `/Users/vives/bird-classifier/` (that's iMac-Claude's repo).
- **Don't** kill Hailo-using processes with `-9`. Use `systemctl --user restart` (graceful) so the Hailo PCIe driver releases the device cleanly.
- **Don't** edit code under `/home/vives/bird-classifier/` directly on the Pi — it'll get clobbered by the next rsync. Edit on iMac at `/Users/vives/bird-classifier-pi/` and rsync.
- **Don't** add `apply_verdict`-style file moves on the Pi. The pi-review system is intentionally pure metadata; the JPG stays where it is so the Live view + Recent strip continue to work.

## Restart flow when something's wrong

If the dashboard misbehaves, restart it (cheap):

```bash
ssh vives@pi5.local "systemctl --user restart bird-dashboard"
```

If detection is wrong (no detections, wrong-looking snapshots), check the pipeline log first — most failures show up as ffmpeg stalls or Hailo errors. Then `systemctl --user restart bird-pipeline`.

If the Pi can't even SSH, you've got a hardware / boot problem; reach for the SD card fallback (see `~/docs/bird-observatory/historical/35-pi5-prep-runbook.md` for the BOOT_ORDER and rpi-clone story).

## Future deployment moves

- **GitHub remote.** Pi-side repo currently has only `imac-origin` (renamed to prevent accidental pushes to David's iMac GitHub). When David wants the Pi-side on GitHub independently, add a fresh `origin` pointing at the new repo.
- **CI / hooks.** None right now; tests run locally with pytest. If the Pi side grows enough to warrant CI, the test suite at `tests/pipeline/test_hailo_*.py` has fakes for `hailo_platform` and runs without HW — viable on a GitHub Actions Linux runner.
- **`apt install` automation.** A real "Pi from flash to live" runbook would scriptize the apt + venv steps from `35-pi5-prep-runbook.md`. We're punting on that until the second Pi is built.
