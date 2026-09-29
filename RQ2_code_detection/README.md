# RQ2 — Code-based detection of privacy-policy display (LLM + ML)

**Question:** can we decide, *from an app's code alone*, whether it shows its privacy
policy on the Health Connect rationale screen? RQ2 compares two static approaches:

- **`llm_agentic/`** — LLM analysts that read the decompiled rationale activity (RA) and
  reason about it (baseline, RA-ReAct, Baseline+RA-RAG).
- **`ml_embedding/`** — a classic text-embedding classifier (LogReg / RandomForest / SVM).

Label convention differs between the two sub-tasks (see each section) — read carefully.

---

## Part A — `ml_embedding/` (embedding classifier)

Pipeline: **decompiled Java text → MiniLM embedding → P/N classifier**.
**Positive class `P` = VIOLATION** (policy NOT shown); `N` = compliant.

```
encode.py     java_text/*.txt            -> embeddings JSON  (384-dim MiniLM)
add_label.py  embeddings + labels CSV    -> labeled embeddings JSON
train.py      labeled embeddings         -> trained model (5-fold CV metrics)
predict.py    model + embeddings         -> predictions CSV
```

### Quickstart (runs on the 20-app sample out of the box)

```bash
cd ml_embedding
pip install scikit-learn pandas joblib numpy          # (+ sentence-transformers torch for encode.py)

# 1. train on the 20 pre-embedded labeled apps (RandomForest; try lr / svm too)
python train.py --algo rf

# 2. predict on the (unlabeled) sample
python predict.py --model-path models_saved/rf/rf_hc_apps_grid.joblib
```

To go from raw code text yourself:

```bash
python encode.py                       # ../sample_data/java_text/*.txt -> embeddings
python add_label.py                    # attach P/N from ../sample_data/sample_ground_truth_20.csv
python train.py --algo lr
```

> The 20-app set is a **smoke-test** (10 P + 10 N); metrics on it are not meaningful.
> The paper trained on **654** labeled apps — reported cross-validated accuracy was
> ~0.81 (LogReg), with RF/SVM comparable (see the docstrings in `train.py`).

Inputs in `../sample_data/`: `java_text/` (20 `.txt`), `embeddings_labeled_20.json`,
`embeddings_unlabeled_20.json`.

---

## Part B — `llm_agentic/` (LLM analysts)

Each model folder (`gemini`, `gpt-5-mini`, `gpt-oss-120b`, `claude-haiku-4-5`, `llama`,
`qwen3-next-80b-a3b-instruct`) has the same four pipelines:

| pipeline | script (folder) | what it does |
|---|---|---|
| **Baseline** | `baseline/hc_baseline_singleagent_*.py` | one LLM call on the RA source only |
| **Baseline + RA-RAG** | `baseline_online_training/hc_baseline_online_*.py` | Baseline + a retrieved "blunder-book" lesson (RA-embedding similarity) |
| **RA-ReAct** | `ReAct/hc_ReAct_*.py` | follow referenced classes hop-by-hop until the policy display is resolved |
| **RA-ReAct + RA-RAG** | `online_training/hc_multi_agent_*.py` | RA-ReAct following combined with the retrieved RA-RAG lesson (analyst + reflective-practitioner) |

Here the LLM answers **`Answer1 = "Yes"`** when the policy **is** shown (compliant) and
`"No"` when it is not — the *opposite* polarity to the ML `P` label. `evaluate.py` maps
`Answer1=="No"` to the positive class VIOLATION when scoring.

### Setup

- **Input:** JADX-decompiled APK trees (`<pkg>/resources/AndroidManifest.xml` +
  `<pkg>/sources/...`). Two ready-made sample apps are in
  `llm_agentic/sample_decompiled_apps/` (see its README).
- **API keys** (only the model you run): `GEMINI_API_KEY`, `OPENAI_API_KEY`,
  `NVIDIA_API_KEY` (gpt-oss / llama / qwen), `ANTHROPIC_API_KEY`. Endpoints/model IDs are
  set at the top of each script.

### Quickstart

```bash
cd llm_agentic

# (no API key) confirm the RA locator sees the sample apps:
python utils/hc_extractor.py --input sample_decompiled_apps \
       --applist sample_decompiled_apps/sample_apps_2.txt --output _loc --locate-only --verbose

# baseline single-agent on the 2 sample apps (needs GEMINI_API_KEY):
python gemini/baseline/hc_baseline_singleagent_gemini.py \
       --input sample_decompiled_apps --applist sample_decompiled_apps/sample_apps_2.txt \
       --output gemini/output/baseline/sample

# RA-ReAct (follows E2.I for com.bosch.ebike):
python gemini/ReAct/hc_ReAct_gemini.py \
       --input sample_decompiled_apps --applist sample_decompiled_apps/sample_apps_2.txt \
       --output gemini/output/ReAct/sample

# Baseline+RA-RAG (retrieval-augmented; uses ../../ra_embeddings.csv + ../../ground_truth_categorized.csv):
python gemini/baseline_online_training/hc_baseline_online_gemini.py \
       --input sample_decompiled_apps --applist sample_decompiled_apps/sample_apps_2.txt \
       --output gemini/output/baseline-online/sample
```

To run the full 673-app study, point `--input` at your decompiled corpus and
`--applist` at a package list (e.g. the packages in `../../data/ground_truth_full_673.csv`).

### Scoring

`evaluate.py` reads each model's `output/<pipeline>/673_app_list/llm_res.json`, compares
to `ground_truth.csv`, and writes a metrics table. Set `LLM_BASE` if your outputs live
elsewhere.

Shared assets at `llm_agentic/` root: `ground_truth.csv`, `ground_truth_categorized.csv`,
`ra_embeddings.csv` (RA-source embeddings for RA-RAG retrieval), `utils/hc_extractor.py`,
per-model `prompt/` folders.

Worked before/after examples of RA-ReAct and RA-RAG correcting the baseline are in
`case_studies/` (one file per model, baseline-wrong → method-right).
