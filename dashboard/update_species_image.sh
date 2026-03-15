#!/bin/bash
# Update a species image from All About Birds / Macaulay Library.
#
# Usage:
#   ./update_species_image.sh "Downy Woodpecker" https://www.allaboutbirds.org/guide/Downy_Woodpecker/photo-gallery/60397941
#   ./update_species_image.sh "Downy Woodpecker" 60397941
#   ./update_species_image.sh "Downy Woodpecker" 60397941 female
#
# The third argument (female) is optional — creates a _female variant.

set -eu

IMAGES_DIR="$(cd "$(dirname "$0")/species_images" && pwd)"

if [ $# -lt 2 ]; then
  echo "Usage: $0 \"Species Name\" <asset-id-or-url> [female]"
  echo ""
  echo "Examples:"
  echo "  $0 \"House Finch\" https://www.allaboutbirds.org/guide/House_Finch/photo-gallery/136083061"
  echo "  $0 \"House Finch\" 136083061"
  echo "  $0 \"House Finch\" 136083061 female"
  exit 1
fi

SPECIES="$1"
INPUT="$2"
SEX="${3:-}"

# Extract asset ID from URL or use directly
if echo "$INPUT" | grep -qE '^[0-9]+$'; then
  ASSET_ID="$INPUT"
else
  ASSET_ID=$(echo "$INPUT" | grep -oE '[0-9]+$')
fi

if [ -z "$ASSET_ID" ]; then
  echo "Error: Could not extract asset ID from: $INPUT"
  exit 1
fi

# Build filename: "Song Sparrow" → "Song_Sparrow.jpg", with optional _female suffix
SAFE_NAME=$(echo "$SPECIES" | sed "s/'//g; s/[^a-zA-Z0-9_-]/_/g; s/^_//; s/_$//")
if [ -n "$SEX" ]; then
  FILENAME="${SAFE_NAME}_${SEX}.jpg"
else
  FILENAME="${SAFE_NAME}.jpg"
fi

CDN_URL="https://cdn.download.ams.birds.cornell.edu/api/v1/asset/${ASSET_ID}/1200"
DEST="${IMAGES_DIR}/${FILENAME}"

echo "Downloading: ${SPECIES}${SEX:+ ($SEX)}"
echo "  Asset ID: ${ASSET_ID}"
echo "  CDN URL:  ${CDN_URL}"
echo "  Dest:     ${DEST}"

curl -sL -o "${DEST}" "${CDN_URL}"

SIZE=$(wc -c < "${DEST}" | tr -d ' ')
if [ "$SIZE" -lt 5000 ]; then
  echo "Error: Download too small (${SIZE} bytes) — asset ID may be invalid"
  rm -f "${DEST}"
  exit 1
fi

echo "Done! ${FILENAME} ($(( SIZE / 1024 ))KB)"
