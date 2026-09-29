#!/usr/bin/env python3
"""
predict.py - classify new apps with a trained model.

Loads a saved joblib model and an embeddings JSON (unlabeled is fine) and writes
a CSV of predictions: pkg_name, prediction (P/N), probability_P.

Defaults use the model trained by `train.py --algo rf` and the in-repo unlabeled
20-app sample:

    python predict.py                                   # rf on the sample
    python predict.py --model-path models_saved/lr/lr_hc_apps_grid.joblib \
                      --embeddings ../sample_data/embeddings_unlabeled_20.json \
                      --output predictions.csv

Install:  pip install scikit-learn pandas joblib
"""
import argparse
import json
from pathlib import Path

import joblib
import pandas as pd

HERE = Path(__file__).resolve().parent
DEFAULT_MODEL = HERE / "models_saved" / "rf" / "rf_hc_apps_grid.joblib"
DEFAULT_EMB   = HERE.parent / "sample_data" / "embeddings_unlabeled_20.json"
DEFAULT_OUT   = HERE / "predictions.csv"


def main():
    ap = argparse.ArgumentParser(description="Predict P/N for apps with a trained model.")
    ap.add_argument("--model-path", default=str(DEFAULT_MODEL))
    ap.add_argument("--embeddings", default=str(DEFAULT_EMB))
    ap.add_argument("--output", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    if not Path(args.model_path).is_file():
        raise SystemExit(f"Model not found: {args.model_path}\nTrain one first, e.g. "
                         f"`python train.py --algo rf`.")

    model = joblib.load(args.model_path)
    data = json.loads(Path(args.embeddings).read_text(encoding="utf-8"))
    pkgs = [d["pkg_name"] for d in data]
    X = [d["embedding"] for d in data]

    y_pred = model.predict(X)
    try:
        y_prob = model.predict_proba(X)[:, 1]
    except Exception:
        y_prob = [None] * len(y_pred)

    df = pd.DataFrame({"pkg_name": pkgs,
                       "prediction": ["P" if p == 1 else "N" for p in y_pred],
                       "probability_P": y_prob})
    df.to_csv(args.output, index=False)
    print(f"Wrote {len(df)} predictions -> {args.output}")
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
