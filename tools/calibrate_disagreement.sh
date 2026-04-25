#!/usr/bin/env bash
# calibrate_disagreement.sh — interactive calibration of RC3's disagreement flag.
#
# Pulls a stratified sample from post-watershed classifications (rows that have
# extra_json.lock_time + extra_json.authoritative + extra_json.disagreement),
# opens each annotated JPG one at a time in Preview, prompts you for the
# correct species, and tallies precision/recall of the disagreement flag.
#
# Strata (default 5 from each):
#   A. disagreement=1 + auth_conf < 0.1   (obvious noise)
#   B. disagreement=1 + auth_conf >= 0.1  (interesting case — AIY confidently disagrees)
#   C. disagreement=0 + auth_conf < 0.1   (potential false negative — both wrong but agreed)
#   D. disagreement=0 + auth_conf >= 0.5  (happy path — both confident, agreed)
#
# Output: tally CSV at ./tools/calibration-results-<ISO>.csv

set -euo pipefail

DB="${DB:-/Users/vives/bird-snapshots/logs/classifications.db}"
N_PER_BUCKET="${N_PER_BUCKET:-5}"
WATERSHED_ID="${WATERSHED_ID:-756294}"
ANNOTATED_DIR="${ANNOTATED_DIR:-/Users/vives/bird-snapshots/annotated}"
RESULTS="./tools/calibration-results-$(date -u +%Y%m%dT%H%M%SZ).csv"

if [ ! -f "$DB" ]; then
  echo "ERROR: DB not found at $DB" >&2; exit 1
fi

mkdir -p "$(dirname "$RESULTS")"
echo "id,bucket,lock_species,lock_conf,lock_source,auth_species,auth_conf,disagreement,human_truth,judgment,note" > "$RESULTS"

pick_bucket () {
  local where="$1" bucket="$2"
  sqlite3 "$DB" "
    SELECT id FROM classifications
    WHERE id >= $WATERSHED_ID AND action='classified'
      AND extra_json IS NOT NULL
      AND json_extract(extra_json,'\$.lock_time') IS NOT NULL
      AND $where
    ORDER BY RANDOM()
    LIMIT $N_PER_BUCKET" \
    | awk -v b="$bucket" '{print b","$0}'
}

declare -a PICKS
while IFS= read -r line; do PICKS+=("$line"); done < <(
  pick_bucket "json_extract(extra_json,'\$.disagreement')=1 AND json_extract(extra_json,'\$.authoritative.confidence')<0.1" "A_dis_lowauth"
  pick_bucket "json_extract(extra_json,'\$.disagreement')=1 AND json_extract(extra_json,'\$.authoritative.confidence')>=0.1" "B_dis_higauth"
  pick_bucket "json_extract(extra_json,'\$.disagreement')=0 AND json_extract(extra_json,'\$.authoritative.confidence')<0.1" "C_agr_lowauth"
  pick_bucket "json_extract(extra_json,'\$.disagreement')=0 AND json_extract(extra_json,'\$.authoritative.confidence')>=0.5" "D_agr_higauth"
)

echo "Pulled ${#PICKS[@]} samples across 4 strata."
echo "Results will be written to: $RESULTS"
echo
echo "For each row: I'll open the annotated JPG. You type the species you see."
echo "(Or 'noise'/'no_bird' if there's no real bird in the bbox, or 'multi' for multi-bird,"
echo "or 's' to skip.)"
echo

for entry in "${PICKS[@]}"; do
  bucket="${entry%%,*}"
  id="${entry##*,}"
  meta=$(sqlite3 "$DB" "
    SELECT
      file,
      json_extract(extra_json,'\$.lock_time.species'),
      ROUND(json_extract(extra_json,'\$.lock_time.confidence'),3),
      json_extract(extra_json,'\$.lock_time.source'),
      json_extract(extra_json,'\$.authoritative.species'),
      ROUND(json_extract(extra_json,'\$.authoritative.confidence'),3),
      json_extract(extra_json,'\$.disagreement')
    FROM classifications WHERE id=$id" | tr '|' '\n')
  read -r file lock_sp lock_cf lock_src auth_sp auth_cf dis <<< "$(echo "$meta" | tr '\n' ' ')"

  echo "──────────────────────────────────────────────"
  echo "id $id  [bucket: $bucket]"
  echo "  yard:  $lock_sp ($lock_cf, $lock_src)"
  echo "  AIY:   $auth_sp ($auth_cf)"
  echo "  flag:  disagreement=$dis"
  echo "  file:  $file"
  open "$ANNOTATED_DIR/$file" 2>/dev/null || echo "  (couldn't open in Preview)"
  read -p "  what species (or noise/no_bird/multi/s): " human
  human="${human:-skip}"
  judgment="?"
  case "$human" in
    skip|s) judgment="skipped" ;;
    *)
      # If lock_sp matches → yard correct. If auth_sp matches → AIY correct.
      lock_match="no"; auth_match="no"
      [ "$human" = "$lock_sp" ] && lock_match="yes"
      [ "$human" = "$auth_sp" ] && auth_match="yes"
      judgment="lock=$lock_match,auth=$auth_match"
      ;;
  esac
  read -p "  optional note: " note
  echo "$id,$bucket,\"$lock_sp\",$lock_cf,$lock_src,\"$auth_sp\",$auth_cf,$dis,\"$human\",\"$judgment\",\"$note\"" >> "$RESULTS"
done

echo
echo "──────────────────────────────────────────────"
echo "Done. Results saved to: $RESULTS"
echo
echo "Quick precision/recall for the disagreement flag:"
sqlite3 ":memory:" <<EOF
.mode column
.headers on
CREATE TABLE r(id,bucket,lock_sp,lock_cf,lock_src,auth_sp,auth_cf,dis,human,judgment,note);
.import --csv --skip 1 "$RESULTS" r
SELECT
  bucket,
  COUNT(*) AS n,
  SUM(CASE WHEN judgment='skipped' THEN 1 ELSE 0 END) AS skipped,
  SUM(CASE WHEN instr(judgment,'lock=yes') THEN 1 ELSE 0 END) AS lock_correct,
  SUM(CASE WHEN instr(judgment,'auth=yes') THEN 1 ELSE 0 END) AS auth_correct
FROM r GROUP BY bucket;
EOF
