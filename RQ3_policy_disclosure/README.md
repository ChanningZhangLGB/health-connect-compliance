# RQ3 — Do privacy policies disclose the requested permissions?

**Question:** for each Health Connect / Android permission an app requests, does its
privacy-policy **text** (1) explicitly disclose collecting that data, and (2) explain
**why**? RQ3 uses an LLM to label every (app, permission) pair from the policy text alone.

## Labels

For each requested permission the model returns one of:

- `<permission>_not_mentioned` — policy never explicitly discloses this data (Step 1 = No)
- `<permission>_specific` + `<permission>_rationale_no` — disclosed, but no purpose given
- `<permission>_specific` + `<permission>_rationale_yes` — disclosed **and** purpose given

plus the exact supporting quotations (`permission_evidence`, `rationale_evidence`). The
full instruction is embedded verbatim in `rq3_pp_label.py` (`SYSTEM_PROMPT` + `TASK_PROMPT`;
also in `prompt.docx`).

## Input format

A JSON array; each record:

```json
{
  "id": "...",
  "packagename": "com.example.app",
  "requested_permission": ["READ_STEPS", "READ_HEART_RATE", "..."],
  "label": ["read_steps_specific", "read_steps_rationale_yes", "..."],   // human GT
  "privacy policy": "<full policy text>"
}
```

`sample_data/sample_admin_labels_20.json` holds 20 such records (one per sample app).

## Requirements

- `pip install openai`
- `NVIDIA_API_KEY` — the three open models are served via the NVIDIA endpoint
  (`gptoss` → gpt-oss-120b, `llama` → llama-3.3-70b, `qwen` → qwen3-next-80b).

## Usage

```bash
# label the 20-record sample with GPT-oss-120b
python rq3_pp_label.py --model gptoss \
       --input sample_data/sample_admin_labels_20.json \
       --output output/gpt-oss-120b --resume

# same for the other models
python rq3_pp_label.py --model llama --input sample_data/sample_admin_labels_20.json --output output/llama --resume
python rq3_pp_label.py --model qwen  --input sample_data/sample_admin_labels_20.json --output output/qwen  --resume
```

Each run writes `output/<model>/llm_res.json` (incrementally, resumable) plus per-app raw
logs. `--limit N` processes only the first N records; `--max-tokens` caps the answer.

## Scoring

```bash
# summarize every output/<model>/llm_res.json against the `label` GT (Step-1 / Step-2 metrics)
python eval_summarize.py     # -> result/summary_metrics.csv

# compare one model's predictions against the GT, per (app, permission)
python eval_compare.py --gt sample_data/sample_admin_labels_20.json \
                       --pred output/gpt-oss-120b/llm_res.json
```

`eval_summarize.py` defaults to the in-repo sample and `output/`; override with env vars
`RQ3_GT`, `RQ3_OUTPUT`, `RQ3_RESULT` to score the full study.

## Files

| path | description |
|---|---|
| `rq3_pp_label.py` | the LLM labeling driver (prompt embedded) |
| `prompt.docx` | the prompt, as authored |
| `eval_summarize.py`, `eval_compare.py` | metric computation / model comparison |
| `sample_data/sample_admin_labels_20.json` | 20-record sample input (with human `label` GT) |
