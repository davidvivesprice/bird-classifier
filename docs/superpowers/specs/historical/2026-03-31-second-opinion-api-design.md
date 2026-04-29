> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Second Opinion API — Design Spec

## Problem

Sparrows and similar-looking species are genuinely hard to tell apart from feeder camera photos. The AIY classifier gets confused between Song Sparrow, Lincoln's Sparrow, White-crowned Sparrow, House Finch, etc. David needs a second opinion from a better model when he's unsure.

## Goal

Add iNaturalist Computer Vision API as a second opinion, triggered manually via button click. Show AIY prediction vs iNaturalist prediction side by side. Rate-limited to stay within iNaturalist's acceptable use.

## Scope

- **Phase 1 (this spec):** iNaturalist API integration, manual trigger only
- **Phase 2 (future):** Local HuggingFace Vision Transformer model, replaces API

## Architecture

```
User clicks "🔍 2nd Opinion" button
    ↓
POST /api/review/second-opinion/{filename}
    ↓
1. Crop bird from classified image (existing)
2. Save crop to ~/bird-snapshots/second-opinion/ (existing)
3. Send crop to iNaturalist score_image API
4. Return both predictions to frontend
    ↓
UI shows: "AIY says: Song Sparrow (85%)" vs "iNaturalist says: Lincoln's Sparrow (92%)"
```

## iNaturalist API Details

**Endpoint:** `POST https://api.inaturalist.org/v1/computervisions/score_image`

**Auth:** OAuth2 bearer token (JWT). Register app at https://www.inaturalist.org/oauth/applications

**Request:**
```
Content-Type: multipart/form-data
Authorization: Bearer {token}

image: bird_crop.jpg
lat: 41.35
lng: -70.74
observed_on: 2026-03-31
taxon_id: 3 (Aves — restrict to birds only)
```

**Response:**
```json
{
  "results": [
    {
      "taxon": {
        "name": "Melospiza lincolnii",
        "preferred_common_name": "Lincoln's Sparrow",
        "rank": "species"
      },
      "combined_score": 0.92,
      "vision_score": 0.88,
      "frequency_score": 0.95
    }
  ]
}
```

## Rate Limiting

- **Only fires on button click** — never automatic
- **Max 1 request per 5 seconds** (server-side throttle)
- **Cache results** — same file always returns cached result, no re-query
- **Expected volume:** 10-50 per day during active review sessions
- **iNaturalist limit:** ~60/min (we'll use <1/min)

## API Token Storage

Token stored in `~/.bird-observatory-env` alongside the Unifi API key:
```
INATURALIST_API_TOKEN=eyJhbGciOi...
```

Read by `scripts/run-with-env.sh` wrapper (already in place for Unifi key).

## Updated Endpoint

`POST /api/review/second-opinion/{filename}` now:

1. Crops the bird (existing)
2. Saves to second-opinion folder (existing)
3. Checks cache — if already queried, return cached result
4. Sends crop to iNaturalist API with location + date
5. Caches the result in a JSON sidecar file
6. Returns both predictions:

```json
{
  "status": "ok",
  "saved": "Song_Sparrow_2026-03-31_10-15-00.jpg",
  "our_prediction": {
    "species": "Song Sparrow",
    "confidence": 0.85
  },
  "inaturalist": {
    "top1": {"species": "Lincoln's Sparrow", "score": 0.92},
    "top3": [
      {"species": "Lincoln's Sparrow", "score": 0.92},
      {"species": "Song Sparrow", "score": 0.78},
      {"species": "Swamp Sparrow", "score": 0.45}
    ]
  }
}
```

## Frontend Changes

The "2nd Opinion" button behavior:

1. Click → button shows spinner
2. API returns → show result inline below the image:

```
┌─────────────────────────────────┐
│  Our classifier: Song Sparrow   │
│  iNaturalist:    Lincoln's      │  ← highlighted if different
│                  Sparrow (92%)  │
│  [Use iNat Result] [Keep Ours]  │
└─────────────────────────────────┘
```

"Use iNat Result" = submit correction with iNaturalist's species.
"Keep Ours" = dismiss the panel.

## Fallback

If iNaturalist API is down or token invalid:
- Still save the crop (existing behavior)
- Show "API unavailable — crop saved for manual Merlin ID"
- Never block the review workflow

## Files

| File | Changes |
|------|---------|
| `dashboard/api.py` | Update second-opinion endpoint with iNaturalist call + cache |
| `dashboard/index.html` | Show comparison result inline, add Use/Keep buttons |
| `~/.bird-observatory-env` | Add INATURALIST_API_TOKEN |

## Setup Required

1. David creates iNaturalist OAuth app at https://www.inaturalist.org/oauth/applications
2. Gets API token via OAuth flow
3. Adds token to `~/.bird-observatory-env`

## Success Criteria

1. Click "2nd Opinion" → see iNaturalist's prediction in <3 seconds
2. Results cached — clicking again returns instantly
3. If API is down, crop still saved, no error in review workflow
4. Rate stays under 60/min (realistically 1-2/min max)