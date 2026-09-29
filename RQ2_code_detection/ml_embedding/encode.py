#!/usr/bin/env python3
"""
encode.py - turn per-app decompiled Java text into sentence embeddings.

Each input .txt file is one app (filename stem = package name). We embed the
whole file with sentence-transformers/all-MiniLM-L6-v2 (384-dim, normalized)
and write a JSON array of {"pkg_name", "embedding"} records that train.py /
predict.py consume.

Defaults point at the in-repo 20-app sample so this runs out of the box:

    python encode.py
    python encode.py --input-dir ../sample_data/java_text --output my_embeddings.json

Install:  pip install -U sentence-transformers torch
"""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_IN  = HERE.parent / "sample_data" / "java_text"
DEFAULT_OUT = HERE.parent / "sample_data" / "embeddings_encoded_20.json"


def load_texts(txt_paths):
    texts, names = [], []
    for p in txt_paths:
        try:
            s = p.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            print(f"[skip] {p.name}: {e}")
            continue
        if not s.strip():
            print(f"[skip-empty] {p.name}")
            continue
        texts.append(s)
        names.append(p.stem)      # pkg name = file name without .txt
    return texts, names


def main():
    ap = argparse.ArgumentParser(description="Embed decompiled-Java text into MiniLM vectors.")
    ap.add_argument("--input-dir", default=str(DEFAULT_IN),
                    help="folder of <package>.txt files (default: repo 20-app sample)")
    ap.add_argument("--output", default=str(DEFAULT_OUT),
                    help="output embeddings JSON")
    ap.add_argument("--model", default="sentence-transformers/all-MiniLM-L6-v2")
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--recursive", action="store_true")
    args = ap.parse_args()

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        sys.exit("Please install: pip install -U sentence-transformers torch")

    in_dir = Path(args.input_dir)
    pattern = "**/*.txt" if args.recursive else "*.txt"
    files = sorted(in_dir.glob(pattern))
    if not files:
        sys.exit(f"No .txt files found under: {in_dir}")

    model = SentenceTransformer(args.model)  # auto CPU/GPU
    records = []
    for i in range(0, len(files), args.batch_size):
        texts, names = load_texts(files[i:i + args.batch_size])
        if not texts:
            continue
        embs = model.encode(texts, batch_size=args.batch_size, convert_to_numpy=True,
                             normalize_embeddings=True, show_progress_bar=False)
        for pkg, emb in zip(names, embs):
            records.append({"pkg_name": pkg, "embedding": emb.tolist()})

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)
    print(f"Encoded {len(records)}/{len(files)} apps -> {args.output}")
    if records:
        print(f"Embedding dim: {len(records[0]['embedding'])}")


if __name__ == "__main__":
    main()
