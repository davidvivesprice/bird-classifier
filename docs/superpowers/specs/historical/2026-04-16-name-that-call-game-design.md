> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Name That Call — Game Design Spec

**Date:** 2026-04-16
**Status:** Approved by David
**Author:** Claude (Opus 4.6) + David

---

## Overview

A bird call identification game using real BirdNET recordings from the Vives Bird Observatory. A clip plays, four species are shown, the player picks one. Short rounds, repeatable, stats tracked per player.

## Data Source

- **Table:** `notes` in `birdnet_local.db`
- **Filter:** `has_clip = 1 AND confidence >= 0.9`
- **Minimum:** species must have 3+ qualifying clips to be included in the game
- **Clips served via:** `/api/birdnet-clip-enhanced/{path}` (bandpass + loudness normalized)

## Game Flow

1. Player selects or enters their name
2. Round starts — 10 clips per round
3. For each clip:
   a. Clip auto-plays
   b. Four species buttons shown (1 correct, 3 wrong)
   c. Player taps an answer
   d. Instant feedback: correct (green flash + species photo) or wrong (red flash + correct species shown)
   e. Brief pause (~1.5s), then next clip
4. After 10 clips: summary screen with score, streak, which species were missed
5. "Play Again" button starts a new round

## Wrong Answer Selection

Each round uses a mix of easy and hard wrong answers:

**Easy (random):** Pick from any species in the game pool that isn't the correct answer. A Blue Jay question might show House Finch, American Crow, Mourning Dove — obviously different sounds.

**Hard (confusable):** Pick from a lookup table of species that sound similar. Defined in code:

```python
CONFUSABLE_GROUPS = [
    # Sparrows — similar chip notes and trills
    ["Song Sparrow", "White-throated Sparrow", "Chipping Sparrow", "House Finch"],
    # Woodpeckers — drumming and calls
    ["Downy Woodpecker", "Hairy Woodpecker", "Red-bellied Woodpecker"],
    # Corvids — caws and calls
    ["American Crow", "Fish Crow", "Blue Jay"],
    # Wrens and warblers — complex songs
    ["Carolina Wren", "House Wren"],
    # Raptors
    ["Red-tailed Hawk", "Cooper's Hawk"],
    # Blackbirds
    ["Red-winged Blackbird", "Common Grackle", "Brown-headed Cowbird"],
]
```

**Selection algorithm per question:**
- 50% chance: 1 confusable + 2 random
- 30% chance: 2 confusable + 1 random (harder)
- 20% chance: 3 random (easy breather round)

As the player's per-species accuracy improves past 80%, shift the ratio toward more confusable wrong answers for that species.

## Difficulty Curve

**Clip selection per round:**
- Clips are drawn from a pool filtered by confidence threshold
- New/low-accuracy players: threshold >= 0.95 (cleanest recordings)
- After 5+ rounds with 70%+ accuracy: threshold >= 0.85
- After 10+ rounds with 80%+ accuracy: threshold >= 0.75 (noisier, harder)

**Species weighting:**
- Species the player gets wrong are weighted higher for future rounds (spaced repetition)
- Species mastered (5+ correct in a row) appear less frequently

## Player Identity

- **First visit:** text input for name, stored in a cookie (`bird_game_player`)
- **Return visit:** cookie auto-selects player, dropdown shows all known players + "New Player"
- **No auth:** cookie-based only, no passwords
- **Cookie lifetime:** 1 year

## Data Model

New SQLite table in `birdnet_local.db` (or a separate `game.db` — TBD based on preference):

```sql
CREATE TABLE game_players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    total_rounds INTEGER DEFAULT 0,
    total_correct INTEGER DEFAULT 0,
    total_answered INTEGER DEFAULT 0,
    best_streak INTEGER DEFAULT 0,
    current_difficulty TEXT DEFAULT 'easy'   -- easy, medium, hard
);

CREATE TABLE game_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER REFERENCES game_players(id),
    played_at TEXT DEFAULT (datetime('now')),
    score INTEGER,          -- correct out of 10
    streak INTEGER,         -- best streak in this round
    difficulty TEXT          -- easy, medium, hard
);

CREATE TABLE game_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER REFERENCES game_rounds(id),
    clip_id INTEGER,        -- notes.id from birdnet_local.db
    correct_species TEXT,
    chosen_species TEXT,
    is_correct INTEGER,
    response_time_ms INTEGER
);
```

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/game/start` | POST | Start a round. Body: `{player_name}`. Returns: `{round_id, player_id, questions: [{clip_url, choices: [4 species], correct_index}]}` |
| `/api/game/answer` | POST | Submit an answer. Body: `{round_id, question_index, chosen_species, response_time_ms}`. Returns: `{is_correct, correct_species, streak}` |
| `/api/game/round-summary` | GET | `?round_id=N`. Returns: `{score, streak, answers: [...], species_photos}` |
| `/api/game/leaderboard` | GET | Returns: `{players: [{name, best_streak, accuracy_pct, total_rounds}]}` |
| `/api/game/player-stats` | GET | `?player_name=X`. Returns: `{per_species_accuracy, rounds_played, best_streak, difficulty}` |

**Question generation (`/api/game/start`):**
1. Query eligible clips: `SELECT * FROM notes WHERE has_clip = 1 AND confidence >= ? AND common_name IN (species with 3+ clips)`
2. Pick 10 random clips, no repeat species in a row
3. For each clip, generate 3 wrong answers using the easy/hard mix algorithm
4. Return the full round definition (client doesn't need to make 10 separate requests)

**Answer validation is server-side** — the correct answer is stored in the round data, not sent to the client. Prevents cheating.

## UI Design

Standalone page at `/game`. Mobile-first.

**Name entry screen:**
- "What's your name?" with text input
- Below: existing player names as tappable chips
- Enter/tap → cookie set, game starts

**Game screen:**
- Top bar: player name, round progress (3/10), streak counter with fire emoji
- Center: species photo area (hidden until answer, then reveals correct species)
- Audio: clip plays automatically, replay button available
- Bottom: 4 large answer buttons, species names, tappable
- On correct: button flashes green, species photo slides in, streak increments
- On wrong: chosen button flashes red, correct button highlights green, correct species photo shown

**Summary screen:**
- Score: "7 / 10"
- Best streak this round
- List of missed species with their photos (learning moment)
- "Play Again" button
- Link to leaderboard

**Leaderboard:**
- Table: rank, name, best streak, accuracy %, rounds played
- Highlight current player

## Technical Notes

- Use separate `game.db` to keep game data independent of bird system
- All game logic server-side (question generation, answer validation, scoring)
- Client is pure HTML/CSS/JS — no framework, matches observatory aesthetic
- Enhanced clips for clean audio (bandpass + dynaudnorm)
- Species photos from existing `/api/species-image/{name}` cache