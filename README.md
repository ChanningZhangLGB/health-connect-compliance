# Health Connect Privacy-Policy Compliance

**An Empirical Study of Privacy Policy Display Compliance in Health Connect Apps**

Health Connect is Android's platform layer for sharing health data between apps. Every app
that requests Health Connect permissions must let users open its privacy policy from the
permission screen, and that policy should say what health data the app collects and why.
This repository contains the code and data for a large-scale audit of whether real apps do
both: a runtime UI study, code-level detectors built on LLM agents and an embedding
classifier, and an LLM-based analysis of the policy texts themselves.

## How it works

The study answers three research questions, each a self-contained sub-project with a
runnable 20-app sample:

| RQ | Question | Method | Folder |
|----|----------|--------|--------|
| **RQ1** | At runtime, does tapping *"Read privacy policy"* on the HC permission screen actually show a policy? | Dynamic ADB UI automation → screenshots | [`RQ1_ui_accessibility/`](RQ1_ui_accessibility/) |
| **RQ2** | From the app's **code** alone, can we detect whether the policy is shown? | LLM agents (**Baseline**, **Baseline + RA-RAG**, **RA-ReAct**, **RA-ReAct + RA-RAG**) **and** an ML embedding classifier | [`RQ2_code_detection/`](RQ2_code_detection/) |
| **RQ3** | Does the policy **text** disclose each requested permission (and explain why)? | LLM per-permission labeling of policy text | [`RQ3_policy_disclosure/`](RQ3_policy_disclosure/) |

The same 20 sample apps appear in all three, so one app can be followed end to end: its
runtime screenshot (RQ1), its code-level verdict (RQ2) and its policy-disclosure labels (RQ3).
Each sub-folder has its own README with copy-pasteable commands.

## Results

- **Compliance.** Of the **673** Health Connect apps with ground-truth labels, **369
  (54.8%)** do not show a privacy policy from the Health Connect permission screen; 304 do.
  The labels are in [`data/ground_truth_full_673.csv`](data/ground_truth_full_673.csv).
- **Scale.** RQ1 sweeps about 1,000 apps on a real device; RQ2 evaluates six LLMs under four
  agent pipelines plus an embedding classifier on the 673 labelled apps; RQ3 labels 224
  (app, policy) records permission by permission.

Per-model detection accuracy and the disclosure rates are reported in the paper.

## Data

`data/` holds the full compliance labels for all 673 apps and the shared 20-app sample
(10 compliant, 10 non-compliant). Each RQ folder carries its own sample inputs: expected
screenshots (RQ1), decompiled code excerpts and embeddings (RQ2), and policy-permission
records (RQ3). The 20-app samples make every pipeline runnable; they are not the evaluation
set (see [Scope of the samples](#scope-of-the-samples)).

## Models

| Model | Provider | Used in |
|---|---|---|
| `gemini-2.5-flash-lite` | Google (OpenAI-compatible endpoint) | RQ2 |
| `gpt-5-mini` (reasoning effort medium) | OpenAI | RQ2 |
| `claude-haiku-4-5-20251001` | Anthropic | RQ2 |
| `openai/gpt-oss-120b` | NVIDIA | RQ2, RQ3 |
| `meta/llama-3.3-70b-instruct` | NVIDIA | RQ2, RQ3 |
| `qwen/qwen3-next-80b-a3b-instruct` | NVIDIA | RQ2, RQ3 |

Temperature 0 (except `gpt-5-mini`, which takes a reasoning effort instead) and at most 4,096
output tokens; see
[`RQ2_code_detection/llm_agentic/model_settings.csv`](RQ2_code_detection/llm_agentic/model_settings.csv).

## Repository layout

```
health-connect-compliance/
├── README.md                 ← you are here
├── requirements.txt          ← Python deps (one file for all RQs)
├── data/
│   ├── sample_apps_20.txt            # the shared 20-app sample (10 compliant + 10 violation)
│   ├── sample_ground_truth_20.csv    # package, ground_truth, compliance  (Yes = policy shown)
│   └── ground_truth_full_673.csv     # full study labels (reference)
│
├── RQ1_ui_accessibility/
│   ├── scripts/hc_ui_accessibility_test.ps1   # ADB UI driver (install → navigate → screenshot → uninstall)
│   ├── sample_apps/sample_apps_20.txt
│   └── sample_output_screenshots/            # 20 expected result screenshots
│
├── RQ2_code_detection/
│   ├── ml_embedding/         # encode.py, add_label.py, train.py (lr|rf|svm), predict.py
│   ├── llm_agentic/          # 6 models × {Baseline, Baseline+RA-RAG, RA-ReAct, RA-ReAct+RA-RAG} + evaluate.py
│   │   └── sample_decompiled_apps/   # 2 reconstructed apps for an end-to-end smoke test
│   ├── case_studies/         # worked baseline-wrong → method-right examples
│   └── sample_data/          # 20 java_text/*.txt, embeddings (labeled + unlabeled), gt subset
│
└── RQ3_policy_disclosure/
    ├── rq3_pp_label.py       # LLM per-permission disclosure labeling (prompt embedded)
    ├── eval_summarize.py, eval_compare.py
    └── sample_data/sample_admin_labels_20.json   # 20 policy+permission records
```

The **same 20 apps** appear across all three RQs, so you can follow one app end-to-end:
its runtime screenshot (RQ1), its code-level verdict (RQ2), and its policy-text disclosure
labels (RQ3).

## Setup

```bash
git clone https://github.com/ChanningZhangLGB/health-connect-compliance.git && cd health-connect-compliance
python -m venv .venv && . .venv/Scripts/activate      # Windows; use .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
```

External tools (not pip): **ADB / Android platform-tools** for RQ1, and **JADX** if you
want to decompile your own APKs for RQ2's LLM pipeline. See per-RQ READMEs.

### API keys (RQ2 llm_agentic and RQ3 only)

Set the key(s) for the model(s) you actually run:

| env var | models |
|---|---|
| `GEMINI_API_KEY` | Gemini-2.5-flash-lite |
| `OPENAI_API_KEY` | GPT-5-mini |
| `NVIDIA_API_KEY` | GPT-oss-120b, Llama-3.3-70B, Qwen3-80B (served via NVIDIA) |
| `ANTHROPIC_API_KEY` | Claude-Haiku-4.5 |

RQ1 and RQ2's ML classifier need **no** API keys.

## Reproducing

### 5-minute quickstart (no API keys)

**RQ2: ML classifier on the 20-app sample:**
```bash
cd RQ2_code_detection/ml_embedding
python train.py --algo rf
python predict.py --model-path models_saved/rf/rf_hc_apps_grid.joblib
```

**RQ2: LLM RA-locator smoke test (verifies the decompiled-input plumbing):**
```bash
cd RQ2_code_detection/llm_agentic
python utils/hc_extractor.py --input sample_decompiled_apps \
       --applist sample_decompiled_apps/sample_apps_2.txt --output _loc --locate-only --verbose
```

**RQ1: UI accessibility (needs a connected device/emulator):**
```powershell
cd RQ1_ui_accessibility
powershell -ExecutionPolicy Bypass -File .\scripts\hc_ui_accessibility_test.ps1 -Package app.aworld
```

Then add API keys and follow each README to run the LLM analysts (RQ2) and the
policy-disclosure labeler (RQ3) on the samples.

### Labels (read this once)

Two different label polarities appear in the artifact; both are documented where used:

- **Compliance (RQ1, RQ2-LLM, ground truth):** `compliance = Yes` / `Answer1 = "Yes"` means
  the privacy policy **is** shown (compliant).
- **ML positive class (RQ2-ML):** `P` = **VIOLATION** (policy *not* shown); `N` = compliant.
  i.e. the ML "detects violations", so `P` is the opposite polarity of `compliance = Yes`.

### Scope of the samples

The 20-app samples make every pipeline runnable and inspectable, but they are **not** the
evaluation set: metrics on 20 apps are not meaningful. The paper's numbers come from the
full corpus (673 apps for RQ2, 224 policy records for RQ3, ~1000-app UI sweep for RQ1).
Full ground-truth labels are in `data/ground_truth_full_673.csv`.

## Citation

The paper is under review. A BibTeX entry will be added here once it is published.

## License

The code is released under the [MIT License](LICENSE).
