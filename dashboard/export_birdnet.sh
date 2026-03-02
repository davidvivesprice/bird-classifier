#!/bin/bash
# Export BirdNET-Go detection summary to a static JSON file.
# Run via cron on VivesSyn every 5 minutes.
# Output: /volume1/docker/birds-hls/birdnet-data/summary.json
set -eu

BIRDNET_DB="/volume1/@docker/volumes/35bfc1d58780095a9ef22a84b6ca2524ef9b2eea7d690dfc039b2698a275f80b/_data/birdnet.db"
OUTPUT_DIR="/volume1/docker/birds-hls/birdnet-data"
OUTPUT_FILE="${OUTPUT_DIR}/summary.json"

mkdir -p "${OUTPUT_DIR}"

# Query BirdNET SQLite DB and output JSON
sqlite3 "${BIRDNET_DB}" -json "
SELECT
    common_name,
    scientific_name,
    COUNT(*) as count,
    ROUND(AVG(confidence), 3) as avg_confidence,
    MAX(date || ' ' || time) as last_seen
FROM notes
GROUP BY common_name
ORDER BY count DESC;
" > "${OUTPUT_DIR}/species_tmp.json" 2>/dev/null

TOTAL=$(sqlite3 "${BIRDNET_DB}" "SELECT COUNT(*) FROM notes;" 2>/dev/null)
SPECIES_COUNT=$(sqlite3 "${BIRDNET_DB}" "SELECT COUNT(DISTINCT common_name) FROM notes;" 2>/dev/null)

# Build final JSON with metadata
python3 -c "
import json, sys
from datetime import datetime
species = json.load(open('${OUTPUT_DIR}/species_tmp.json'))
result = {
    'total_detections': ${TOTAL},
    'species_count': ${SPECIES_COUNT},
    'species': species,
    'updated': datetime.now().isoformat()
}
json.dump(result, sys.stdout, indent=2)
" > "${OUTPUT_FILE}.tmp" && mv "${OUTPUT_FILE}.tmp" "${OUTPUT_FILE}"

rm -f "${OUTPUT_DIR}/species_tmp.json"
