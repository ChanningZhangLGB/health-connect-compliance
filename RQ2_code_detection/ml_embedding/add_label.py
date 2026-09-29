#!/usr/bin/env python3
"""
add_label.py - attach manual P/N labels to an embeddings JSON.

Reads an embeddings JSON (from encode.py) and a labels CSV, and writes a new
JSON where each record gains a "manual_label" field. Records whose package is
not in the CSV get manual_label = null (and are skipped by train.py).

Label convention (matches the paper's ml_detection): the POSITIVE class P = a
privacy VIOLATION (the app does NOT show its policy on the HC rationale screen);
N = compliant.

The labels CSV must have columns  pkg_name , Manual_Label  (values P or N),
OR pass a ground-truth CSV (package , compliance) with --gt-style, where
compliance "No" (violation) -> P and "Yes" (compliant) -> N.

Defaults regenerate the in-repo labeled sample:

    python add_label.py
    python add_label.py --embeddings my.json --labels my_labels.csv --output my_GT.json
"""
import argparse
import csv
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_EMB = HERE.parent / "sample_data" / "embeddings_unlabeled_20.json"
DEFAULT_GT  = HERE.parent / "sample_data" / "sample_ground_truth_20.csv"
DEFAULT_OUT = HERE.parent / "sample_data" / "embeddings_labeled_20.json"


def load_label_map(path, gt_style):
    m = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            if gt_style:
                pkg = (row.get("package") or "").strip()
                comp = (row.get("compliance") or "").strip().lower()
                if pkg:
                    # positive class P = violation (compliance == "No")
                    m[pkg] = "P" if comp == "no" else "N"
            else:
                pkg = (row.get("pkg_name") or "").strip()
                lab = (row.get("Manual_Label") or "").strip().upper()
                if pkg and lab in {"P", "N"}:
                    m[pkg] = lab
    return m


def main():
    ap = argparse.ArgumentParser(description="Add manual P/N labels to embeddings JSON.")
    ap.add_argument("--embeddings", default=str(DEFAULT_EMB))
    ap.add_argument("--labels", default=str(DEFAULT_GT))
    ap.add_argument("--output", default=str(DEFAULT_OUT))
    ap.add_argument("--gt-style", action="store_true", default=True,
                    help="labels CSV is a ground_truth.csv (package,compliance). On by default.")
    ap.add_argument("--custom-label-csv", dest="gt_style", action="store_false",
                    help="labels CSV has pkg_name,Manual_Label columns instead.")
    args = ap.parse_args()

    records = json.loads(Path(args.embeddings).read_text(encoding="utf-8"))
    label_map = load_label_map(args.labels, args.gt_style)

    missing = 0
    for rec in records:
        pkg = rec.get("pkg_name")
        if pkg in label_map:
            rec["manual_label"] = label_map[pkg]
        else:
            rec["manual_label"] = None
            missing += 1

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)
    print(f"Wrote {args.output} | {len(records)} objects, {missing} without a label")


if __name__ == "__main__":
    main()
