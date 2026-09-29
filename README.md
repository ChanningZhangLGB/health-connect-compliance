# An Empirical Study of Privacy Policy Display Compliance in Health Connect Apps

Code and sample data for our study of whether Android **Health Connect (HC)** apps make
their **privacy policy** reachable and honest. Three research questions, three self-contained
sub-projects, each with a runnable **20-app sample**:

| RQ | Question | Method | Folder |
|----|----------|--------|--------|
| **RQ1** | At runtime, does tapping *"Read privacy policy"* on the HC permission screen actually show a policy? | Dynamic ADB UI automation → screenshots | [`RQ1_ui_accessibility/`](RQ1_ui_accessibility/) |
| **RQ2** | From the app's **code** alone, can we detect whether the policy is shown? | LLM agents (**Baseline**, **Baseline + RA-RAG**, **RA-ReAct**, **RA-ReAct + RA-RAG**) **and** an ML embedding classifier | [`RQ2_code_detection/`](RQ2_code_detection/) |
| **RQ3** | Does the policy **text** disclose each requested permission (and explain why)? | LLM per-permission labeling of policy text | [`RQ3_policy_disclosure/`](RQ3_policy_disclosure/) |

Each sub-folder has its own README with detailed, copy-pasteable commands. This page is the
map and the 5-minute quickstart.

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

## Install

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
| `GEMINI_API_KEY` | Gemini-2.5-flash |
| `OPENAI_API_KEY` | GPT-5-mini |
| `NVIDIA_API_KEY` | GPT-oss-120b, Llama-3.3-70B, Qwen3-80B (served via NVIDIA) |
| `ANTHROPIC_API_KEY` | Claude-Haiku-4.5 |

RQ1 and RQ2's ML classifier need **no** API keys.

## 5-minute quickstart (no API keys)

**RQ2 — ML classifier on the 20-app sample:**
```bash
cd RQ2_code_detection/ml_embedding
python train.py --algo rf
python predict.py --model-path models_saved/rf/rf_hc_apps_grid.joblib
```

**RQ2 — LLM RA-locator smoke test (verifies the decompiled-input plumbing):**
```bash
cd RQ2_code_detection/llm_agentic
python utils/hc_extractor.py --input sample_decompiled_apps \
       --applist sample_decompiled_apps/sample_apps_2.txt --output _loc --locate-only --verbose
```

**RQ1 — UI accessibility (needs a connected device/emulator):**
```powershell
cd RQ1_ui_accessibility
powershell -ExecutionPolicy Bypass -File .\scripts\hc_ui_accessibility_test.ps1 -Package app.aworld
```

Then add API keys and follow each README to run the LLM analysts (RQ2) and the
policy-disclosure labeler (RQ3) on the samples.

## Labels — read this once

Two different label polarities appear in the artifact; both are documented where used:

- **Compliance (RQ1, RQ2-LLM, ground truth):** `compliance = Yes` / `Answer1 = "Yes"` means
  the privacy policy **is** shown (compliant).
- **ML positive class (RQ2-ML):** `P` = **VIOLATION** (policy *not* shown); `N` = compliant.
  i.e. the ML "detects violations", so `P` is the opposite polarity of `compliance = Yes`.

## Scope of the samples

The 20-app samples make every pipeline runnable and inspectable, but they are **not** the
evaluation set — metrics on 20 apps are not meaningful. The paper's numbers come from the
full corpus (673 apps for RQ2, 224 policy records for RQ3, ~1000-app UI sweep for RQ1).
Full ground-truth labels are in `data/ground_truth_full_673.csv`.
