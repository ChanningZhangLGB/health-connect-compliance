#!/usr/bin/env python3
"""
train.py - train a P/N classifier on app embeddings (LogReg / RandomForest / SVM).

Positive class P = a privacy VIOLATION (the app does NOT show its policy on the
HC rationale screen); N = compliant. Uses 5-fold stratified GridSearchCV, reports
cross-validated metrics, and saves the best model (refit on all data) as a joblib.

The estimators and hyper-parameter grids match the paper's ml_detection scripts
(lr.py / rf.py / svm.py). Defaults use the in-repo labeled 20-app sample:

    python train.py --algo lr        # or rf / svm
    python train.py --algo rf --embeddings ../sample_data/embeddings_labeled_20.json --outdir models_saved

NOTE: the 20-app sample is a smoke-test set; the paper trained on 654 labeled
apps (see README for the reported numbers).

Install:  pip install scikit-learn pandas joblib numpy
"""
import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import StratifiedKFold, GridSearchCV, cross_val_predict
from sklearn.metrics import classification_report, accuracy_score, f1_score, confusion_matrix

HERE = Path(__file__).resolve().parent
DEFAULT_EMB = HERE.parent / "sample_data" / "embeddings_labeled_20.json"
DEFAULT_OUT = HERE / "models_saved"
RSEED = 42


def load_xy(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    X, y, names = [], [], []
    for o in data:
        emb, lab = o.get("embedding"), o.get("manual_label")
        if emb is None or lab is None:
            continue
        lab = str(lab).strip().upper()
        if lab not in {"P", "N"}:
            continue
        X.append(emb)
        y.append(1 if lab == "P" else 0)   # P=1, N=0
        names.append(o.get("pkg_name", ""))
    return np.asarray(X, np.float32), np.asarray(y, np.int64), names


def build(algo):
    """Return (estimator, param_grid) matching the paper's configs."""
    if algo == "lr":
        from sklearn.linear_model import LogisticRegression
        est = LogisticRegression(class_weight="balanced", max_iter=5000,
                                 random_state=RSEED, solver="liblinear")
        grid = [
            {"solver": ["liblinear"], "penalty": ["l2"], "C": [0.1, 0.5, 1, 3, 10]},
            {"solver": ["liblinear"], "penalty": ["l1"], "C": [0.1, 0.5, 1, 3, 10]},
            {"solver": ["saga"], "penalty": ["l2"], "C": [0.5, 1, 3]},
            {"solver": ["saga"], "penalty": ["l1"], "C": [0.5, 1, 3]},
            {"solver": ["saga"], "penalty": ["elasticnet"], "C": [1], "l1_ratio": [0.5]},
        ]
        return est, grid
    if algo == "rf":
        from sklearn.ensemble import RandomForestClassifier
        est = RandomForestClassifier(n_estimators=400, class_weight="balanced_subsample",
                                     random_state=RSEED, n_jobs=-1)
        grid = {"max_depth": [None, 20], "min_samples_split": [2, 4],
                "min_samples_leaf": [1, 2], "max_features": ["sqrt", 0.5]}
        return est, grid
    if algo == "svm":
        from sklearn.pipeline import Pipeline
        from sklearn.svm import SVC
        est = Pipeline([("svc", SVC(class_weight="balanced", probability=True))])
        grid = [
            {"svc__kernel": ["linear"], "svc__C": [0.1, 0.5, 1, 3, 10]},
            {"svc__kernel": ["rbf"], "svc__C": [0.5, 1, 3, 10], "svc__gamma": ["scale", 0.01, 0.001]},
        ]
        return est, grid
    raise ValueError(algo)


def main():
    ap = argparse.ArgumentParser(description="Train P/N embedding classifier (lr|rf|svm).")
    ap.add_argument("--algo", choices=["lr", "rf", "svm"], required=True)
    ap.add_argument("--embeddings", default=str(DEFAULT_EMB))
    ap.add_argument("--outdir", default=str(DEFAULT_OUT))
    ap.add_argument("--splits", type=int, default=5)
    args = ap.parse_args()

    outdir = Path(args.outdir) / args.algo
    outdir.mkdir(parents=True, exist_ok=True)

    X, y, names = load_xy(args.embeddings)
    if len(X) == 0:
        raise SystemExit("No labeled samples (need 'embedding' + 'manual_label' P/N).")
    print(f"Loaded {len(X)} samples | P={int(y.sum())} N={int((y == 0).sum())} | dim={X.shape[1]}")

    n_splits = min(args.splits, int(y.sum()), int((y == 0).sum()))
    if n_splits < 2:
        raise SystemExit("Not enough per-class samples for cross-validation.")
    cv = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=RSEED)

    est, grid = build(args.algo)
    gs = GridSearchCV(est, grid,
                      scoring={"accuracy": "accuracy", "f1_macro": "f1_macro",
                               "precision_macro": "precision_macro", "recall_macro": "recall_macro"},
                      refit="f1_macro", cv=cv, n_jobs=-1, verbose=1, return_train_score=False)
    gs.fit(X, y)
    print(f"\nBest params: {gs.best_params_}")
    print(f"Best CV accuracy: {gs.cv_results_['mean_test_accuracy'][gs.best_index_]:.4f}")

    y_pred = cross_val_predict(gs.best_estimator_, X, y, cv=cv, n_jobs=-1)
    acc = accuracy_score(y, y_pred)
    f1m = f1_score(y, y_pred, average="macro")
    report = classification_report(y, y_pred, target_names=["N", "P"], digits=4)
    cm = confusion_matrix(y, y_pred, labels=[0, 1])
    print(f"\n=== Cross-validated report ({args.algo}) ===")
    print(f"Accuracy: {acc:.4f} | F1-macro: {f1m:.4f}\n{report}\n{cm}")

    import joblib
    model_path = outdir / f"{args.algo}_hc_apps_grid.joblib"
    joblib.dump(gs.best_estimator_, model_path)
    pd.DataFrame(gs.cv_results_).to_csv(outdir / f"{args.algo}_cv_results.csv", index=False)
    pd.DataFrame({"pkg_name": names,
                  "true_label": np.where(y == 1, "P", "N"),
                  "pred_label": np.where(y_pred == 1, "P", "N")}).to_csv(
        outdir / f"{args.algo}_cv_predictions.csv", index=False)
    (outdir / f"{args.algo}_cv_report.txt").write_text(
        f"Accuracy: {acc:.4f} | F1-macro: {f1m:.4f}\n\n{report}\n{cm}\n", encoding="utf-8")
    meta = {"algo": args.algo, "best_params": gs.best_params_, "n_samples": int(len(X)),
            "embedding_dim": int(X.shape[1]), "cv_splits": n_splits,
            "cv_accuracy": float(acc), "cv_f1_macro": float(f1m),
            "label_mapping": {"N": 0, "P": 1}}
    (outdir / f"{args.algo}_training_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"\nSaved model -> {model_path}")


if __name__ == "__main__":
    main()
