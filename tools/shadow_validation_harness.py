#!/usr/bin/env python3
"""Phase 1 Shadow Validation Harness.

Compares Pi (Hailo/AIY) classifications against iMac (AIY) classifications
for the same camera feed over a given date. Both systems watch the same
UniFi G3 Dome but run independent pipelines; agreement is measured by
temporal proximity (same bird visit → both systems classify it within
~30 seconds of each other).

Both systems expose /api/recent?limit=N&date=YYYY-MM-DD on their dashboard
port (:8099 by default). This script fetches from both, temporally aligns
events, and computes the three Phase 1 gate metrics.

Usage:
    python3 tools/shadow_validation_harness.py                # yesterday, default URLs
    python3 tools/shadow_validation_harness.py --date 2026-05-01
    python3 tools/shadow_validation_harness.py \\
        --pi-url http://pi5.local:8099 \\
        --imac-url http://localhost:8099 \\
        --output-dir /tmp/shadow_validation_$(date +%Y%m%d)
    python3 tools/shadow_validation_harness.py --final-report

Output (in --output-dir):
    shadow_validation_report.json   key metrics + gate pass/fail
    confusion_matrix.json           Pi species → iMac species → count
    per_track_agreement.csv         one row per matched pair
    per_species_agreement.csv       per-species agreement rates
"""
import argparse
import csv
import json
import sys
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_classifications(base_url: str, date: str, limit: int = 2000) -> list:
    """Fetch classifications from /api/recent?limit=N&date=YYYY-MM-DD."""
    url = f"{base_url.rstrip('/')}/api/recent?limit={limit}&date={date}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        if isinstance(data, list):
            return data
        return data.get("items", data.get("recent", []))
    except urllib.error.URLError as e:
        print(f"  WARN: cannot reach {url}: {e}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  WARN: error fetching {url}: {e}", file=sys.stderr)
        return []


# ── Timestamp parsing ─────────────────────────────────────────────────────────

_TS_FMTS = (
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
)


def parse_ts(ts_str: str) -> float:
    """Parse ISO timestamp string → Unix float seconds. Returns 0.0 on failure."""
    if not ts_str:
        return 0.0
    s = ts_str[:26]  # trim sub-microsecond and timezone suffix
    for fmt in _TS_FMTS:
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


# ── Temporal matching ─────────────────────────────────────────────────────────

def match_events(pi_events: list, imac_events: list, window_sec: float = 30.0) -> list:
    """
    Pair Pi events to iMac events by nearest timestamp within window_sec.

    Each Pi event is matched to at most one iMac event (greedy nearest-first).
    Returns a list of dicts with pi/imac fields and an agrees flag.
    """
    if not pi_events or not imac_events:
        return []

    for ev in pi_events:
        ev["_ts"] = parse_ts(ev.get("timestamp") or ev.get("source_timestamp", ""))
    for ev in imac_events:
        ev["_ts"] = parse_ts(ev.get("timestamp") or ev.get("source_timestamp", ""))

    pi_sorted = sorted((e for e in pi_events if e["_ts"]), key=lambda e: e["_ts"])
    imac_sorted = sorted((e for e in imac_events if e["_ts"]), key=lambda e: e["_ts"])

    matched = []
    used_imac: set = set()

    for pi_ev in pi_sorted:
        pi_ts = pi_ev["_ts"]
        best_j, best_imac, best_delta = None, None, float("inf")

        for j, imac_ev in enumerate(imac_sorted):
            if j in used_imac:
                continue
            delta = abs(imac_ev["_ts"] - pi_ts)
            if delta > window_sec:
                # Since imac_sorted is ordered, once we pass window_sec in the
                # forward direction we can skip; but we still need to check
                # earlier events for the backward direction. A full scan is
                # fine for typical day-sizes (~hundreds of events).
                continue
            if delta < best_delta:
                best_delta = delta
                best_j, best_imac = j, imac_ev

        if best_imac is not None:
            used_imac.add(best_j)
            pi_sp = (pi_ev.get("common_name") or "").strip()
            imac_sp = (best_imac.get("common_name") or "").strip()
            agrees = bool(pi_sp and imac_sp and pi_sp.lower() == imac_sp.lower())
            matched.append({
                "pi_timestamp": pi_ev.get("timestamp"),
                "imac_timestamp": best_imac.get("timestamp"),
                "delta_sec": round(best_delta, 2),
                "pi_species": pi_sp,
                "imac_species": imac_sp,
                "pi_confidence": pi_ev.get("confidence") or pi_ev.get("raw_score"),
                "imac_confidence": best_imac.get("confidence") or best_imac.get("raw_score"),
                "agrees": agrees,
            })

    return matched


# ── Metrics ───────────────────────────────────────────────────────────────────

def compute_report(matches: list, pi_total: int, imac_total: int,
                   window_sec: float) -> dict:
    """Derive all gate metrics and per-species stats from matched pairs."""
    if not matches:
        return {
            "per_track_agreement_ratio": 0.0,
            "intra_frame_agreement": 0.0,
            "regressions_on_rare_species": [],
            "gate_pass": False,
            "total_pi_events": pi_total,
            "total_imac_events": imac_total,
            "matched_pairs": 0,
            "match_rate": 0.0,
            "agreement_window_sec": window_sec,
            "intra_frame_window_sec": 5.0,
            "per_species_agreement": {},
        }

    # Per-track agreement: all matched pairs
    agree_count = sum(1 for m in matches if m["agrees"])
    per_track_ratio = agree_count / len(matches)

    # Intra-frame agreement: tighter 5-second window (same-frame proxy)
    tight = [m for m in matches if m["delta_sec"] <= 5.0]
    intra_agree = sum(1 for m in tight if m["agrees"])
    intra_ratio = (intra_agree / len(tight)) if tight else 0.0

    # Per-species breakdown
    sp_stats: dict = defaultdict(lambda: {"agree": 0, "total": 0})
    for m in matches:
        sp = m["pi_species"] or m["imac_species"]
        if sp:
            sp_stats[sp]["total"] += 1
            if m["agrees"]:
                sp_stats[sp]["agree"] += 1

    per_species = {
        sp: {
            "agree": v["agree"],
            "total": v["total"],
            "rate": round(v["agree"] / v["total"], 3) if v["total"] else 0.0,
        }
        for sp, v in sp_stats.items()
    }

    # Regressions: rare species (≤5 sightings in matched data) with agreement < 0.5
    # Require at least 2 matched pairs to distinguish "rare seen twice, both wrong"
    # from "seen once, unlucky".
    regressions = sorted(
        sp for sp, v in per_species.items()
        if v["total"] <= 5 and v["total"] >= 2 and v["rate"] < 0.5
    )

    gate_pass = (
        per_track_ratio >= 0.90
        and intra_ratio >= 0.90
        and regressions == []
    )

    return {
        "per_track_agreement_ratio": round(per_track_ratio, 4),
        "intra_frame_agreement": round(intra_ratio, 4),
        "regressions_on_rare_species": regressions,
        "gate_pass": gate_pass,
        "total_pi_events": pi_total,
        "total_imac_events": imac_total,
        "matched_pairs": len(matches),
        "match_rate": round(len(matches) / max(pi_total, 1), 4),
        "agreement_window_sec": window_sec,
        "intra_frame_window_sec": 5.0,
        "per_species_agreement": per_species,
    }


# ── Writers ───────────────────────────────────────────────────────────────────

def write_confusion_matrix(matches: list, out_dir: Path) -> None:
    matrix: dict = defaultdict(lambda: defaultdict(int))
    for m in matches:
        matrix[m["pi_species"] or "(none)"][m["imac_species"] or "(none)"] += 1
    out = {k: dict(v) for k, v in sorted(matrix.items())}
    (out_dir / "confusion_matrix.json").write_text(json.dumps(out, indent=2))


def write_per_track_csv(matches: list, out_dir: Path) -> None:
    fields = ["pi_timestamp", "imac_timestamp", "delta_sec", "pi_species",
              "imac_species", "pi_confidence", "imac_confidence", "agrees"]
    with open(out_dir / "per_track_agreement.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(matches)


def write_per_species_csv(report: dict, out_dir: Path) -> None:
    fields = ["species", "agree", "total", "rate"]
    with open(out_dir / "per_species_agreement.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for sp, v in sorted(report["per_species_agreement"].items()):
            w.writerow({"species": sp, **v})


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pi-url", default="http://pi5.local:8099",
                    help="Pi dashboard base URL (default: http://pi5.local:8099)")
    ap.add_argument("--imac-url", default="http://localhost:8099",
                    help="iMac dashboard base URL (default: http://localhost:8099)")
    ap.add_argument("--date", default=None,
                    help="Date to compare YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--limit", type=int, default=2000,
                    help="Max events to fetch per system (default: 2000)")
    ap.add_argument("--window-sec", type=float, default=30.0,
                    help="Temporal match window in seconds (default: 30)")
    ap.add_argument("--output-dir", default="/tmp/shadow_validation",
                    help="Output directory (default: /tmp/shadow_validation)")
    ap.add_argument("--final-report", action="store_true",
                    help="Write PHASE1_FINAL_VALIDATION_REPORT.json instead of the daily file")
    args = ap.parse_args()

    if args.date is None:
        args.date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Shadow validation: {args.date}")
    print(f"  Pi:     {args.pi_url}")
    print(f"  iMac:   {args.imac_url}")
    print(f"  Window: ±{args.window_sec}s  Limit: {args.limit}")

    print("\nFetching Pi classifications...")
    pi_events = fetch_classifications(args.pi_url, args.date, args.limit)
    print(f"  {len(pi_events)} events")

    print("Fetching iMac classifications...")
    imac_events = fetch_classifications(args.imac_url, args.date, args.limit)
    print(f"  {len(imac_events)} events")

    if not pi_events and not imac_events:
        print("\nERROR: No data from either system. Check URLs and --date.", file=sys.stderr)
        return 2

    print("\nMatching events...")
    matches = match_events(pi_events, imac_events, window_sec=args.window_sec)
    print(f"  {len(matches)} matched pairs")

    report = compute_report(matches, len(pi_events), len(imac_events), args.window_sec)
    report["date"] = args.date
    report["generated_at"] = datetime.now(timezone.utc).isoformat()

    # Print summary
    print(f"\n{'─'*52}")
    print(f"  per_track_agreement_ratio  {report['per_track_agreement_ratio']:.3f}  (gate ≥0.90)")
    print(f"  intra_frame_agreement      {report['intra_frame_agreement']:.3f}  (gate ≥0.90)")
    regs = report["regressions_on_rare_species"]
    print(f"  regressions_on_rare_species {'none' if not regs else ', '.join(regs)}")
    print(f"  match_rate                 {report['match_rate']:.3f}")
    gate_str = "✓ PASS" if report["gate_pass"] else "✗ FAIL"
    print(f"\n  Gate: {gate_str}")
    print(f"{'─'*52}")

    report_name = ("PHASE1_FINAL_VALIDATION_REPORT.json"
                   if args.final_report else "shadow_validation_report.json")
    (out_dir / report_name).write_text(json.dumps(report, indent=2))
    write_confusion_matrix(matches, out_dir)
    write_per_track_csv(matches, out_dir)
    write_per_species_csv(report, out_dir)

    print(f"\nOutput → {out_dir}/")
    for f in sorted(out_dir.iterdir()):
        print(f"  {f.name}")

    return 0 if report["gate_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
