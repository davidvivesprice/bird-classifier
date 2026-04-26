# 05 · Dashboard

The Pi dashboard at `dashboard/pi_dash.html` (served by `dashboard/api.py`'s `/` route when `PI_MODE=1`). Everything Pi-only. Phone-friendly via the cloudflared tunnel at `pi5.vivessato.com` (gated behind Cloudflare Access; LAN access at `http://pi5.local:8099` is unauthenticated).

## What's on the page

Top to bottom:

1. **Topbar** — brand mark, `LIVE` chip, `model: <active classifier>`, `detector: Hailo-8L`, `uptime: <h>m`, theme picker.
2. **Stats ribbon** — 4 cards: Classifier (active model name), Detector FPS, Active tracks, Tunnel.
3. **Live panel** — full-width, 16:9 video + bbox/label overlay. Real-time WebRTC from go2rtc.
4. **Recent classifications** — strip of square thumbnails (last 8 by default; "Load older →" expands). Each card has ✓/✗ buttons.
5. **Model Lab** — list of classifier candidates from `/api/models/list`. Each row has an `i` lightbox icon for deep info; the active one is highlighted; detectors carry a "DETECTOR · LAB ONLY" badge.
6. **Audio** — placeholder panel; BirdNET integration pending.
7. **Quick links** — links to `/work`, `/ideas`, `/review-ideas`, `/api/pipeline/health`.

Themes: `observatory` (dark navy/black, gold accent, default), `fieldguide` (cream/sepia paper), `minimalist` (light gray, monospace, sharp corners), `dusk` (warm peach gradient, blurred panels). Persisted in `localStorage["pi-theme"]`.

## Live view — how it differs from the iMac approach

iMac's `/live.html` does HLS playback + a sidecar `segments.json` clock + two Gaussian kernels for label smoothing — beautiful, but ~10 s playback delay. The Pi's Live view trades smoothing math for latency:

- Video: WebRTC direct from go2rtc via the `<video-stream>` custom element (`https://go2rtc.vivessato.com/video-stream.js` over the tunnel, or `http://pi5.local:1984/video-stream.js` on LAN). Sub-second latency, MSE fallback.
- Labels: SSE from `/api/pipeline/events/sse?camera=feeder` — one event per processed frame.
- Smoothing: CSS `transition: transform 240ms cubic-bezier` on the bbox + label DOM nodes. Browser interpolates between SSE updates; no per-track Catmull-Rom or kernel math.
- Track lifecycle: lazy-create DOM nodes per `track_id`; GC after 1.5 s without an update; fade in/out via `opacity` transition.

Coordinate scaling uses `frame_width` / `frame_height` from each SSE event (typ. 640 × 360) into the video element's rendered rect, accounting for `object-fit: contain` letterboxing. So bbox positioning is correct regardless of viewport.

## Model Lab

Driven by `GET /api/models/list` (returns the registry's `CandidateModel.list()` output, including the new `info` and `is_classifier` fields). Each row:

- Active candidate is highlighted (`.model-row.active`) and shows an `ACTIVE` badge.
- Detector candidates (`is_classifier=false`) show `DETECTOR · LAB ONLY`, are visually disabled, and refuse switch attempts at the API.
- Placeholder candidate (`flagship_pending`) shows `coming soon`.
- Every row has a top-right `i` info icon. Click → opens a modal with the candidate's multi-paragraph `info` text (read from the registry).

Switch flow:

1. Click a non-active, non-detector row → `POST /api/models/switch` with `{name}`.
2. The endpoint validates is_classifier, writes `~/.bird-observatory-env`'s `PI_CLASSIFIER=<name>` line, and triggers `subprocess.Popen(["systemctl", "--user", "restart", "bird-pipeline"])` non-blocking. Returns `{ok: true, restart_in_progress: true, active: <name>}` immediately.
3. The dashboard's restart banner shows; `loadModels()` polls until the new `current` matches.

Pre-2026-04-25 the restart was `subprocess.run(timeout=10)` — graceful pipeline shutdown can take 5-15 s (Hailo VDevice release + queue drain + ffmpeg termination), which surfaced as 500 errors to the user even though the restart succeeded. Non-blocking Popen + 1.5 s poll for immediate spawn errors fixes the UX.

## Recent classifications

Driven by `GET /api/pi-review/recent?limit=N` (Pi-native — see `06-pi-review.md`). Each card has:

- Square thumbnail (server crop is square-padded around the bbox + 25 % padding, see `dashboard/api.py:get_image_crop`).
- Species + time + confidence in a small caption.
- ✓ / ✗ buttons that POST to `/api/pi-review/{file}`. Click again on the active verdict to toggle off (DELETE).
- Reviewed cards get a green/red-tinted border per verdict.

The whole strip refreshes every 10 s. "Load older →" bumps the visible window in 8-card increments (cap 400).

## Server-side image crop

`GET /api/image-crop/{filename}` (or `/bird-api/image-crop/...` via the URL-rewrite middleware for cloudflared paths). Steps:

1. Locate the file under `~/bird-snapshots/classified/{species}/`.
2. Pull the bbox from `classifications.db` (`extra_json.best_detection.box`) or use the `?box=x1,y1,x2,y2` query param.
3. Pad 25 % around the bbox, then expand to a SQUARE crop centered on the bird, then clamp/shift the square to fit inside the source image.
4. JPEG encode, return.

The square-crop logic was a bird-visibility fix: previously a 15 %-padded variable-aspect crop + client-side `object-fit: cover` on a 90-px-tall card was chopping top/bottom of perched portrait birds (specifically the head). The square crop + `object-fit: contain` on a square thumbnail keeps the whole bird in frame.

## URL rewrite

`BirdAPIRewriteMiddleware` (in `dashboard/api.py:58`) rewrites `/bird-api/*` → `/api/*` so the dashboard works both through Cloudflare routing (which prefixes `/bird-api/`) and via direct LAN/Tailscale access. Pure ASGI middleware — does NOT extend `BaseHTTPMiddleware` because the base class buffers entire response bodies, breaking SSE / HLS / video streams.

## Pi-only mounts

When `PI_MODE=1`, `dashboard/api.py` mounts the Pi-native review router (`from dashboard.pi_review import router`). When unset, the iMac dashboard runs without it. iMac's review2 system is not used on Pi (per the post-split architecture; see `working/progress/2026-04-25-pi-repo-split.md`).
