# RQ2 Case Studies — where retrieval corrects the baseline

Worked examples in which the **single-pass baseline** produced the **wrong** compliance verdict,
while a **retrieval-augmented** pipeline reached the **correct** verdict. Organized into two
subfolders, one per method.

```
RQ2_case_study/
├── RA-ReAct/            ← Part A: baseline vs RA-ReAct (multi-hop code following)
│   ├── 01_gemini-2.5-flash_com.bosch.ebike.md
│   ├── 02_gpt-5-mini_com.voltathletics.md
│   ├── 03_gpt-oss-120b_homeworkout.md
│   └── 04_qwen3-80b_com.imperon.android.gymapp.md
└── Baseline+RA-RAG/    ← Part B: baseline vs Baseline+RA-RAG (retrieved reference lesson)
    ├── 05_RA-RAG_gemini-2.5-flash_digifit.virtuagym.md
    └── 06_RA-RAG_llama-3.3-70b_com.loreal.smartoffice.md
```

## Task recap (shared by both parts)

For each app we locate the Health Connect *permissions-rationale activity* (RA) and ask the model:

1. Does this code display the app's **privacy policy** when the user clicks the privacy-policy link
   on the Health Connect permissions screen? `Answer1: [Yes/No]`
2. If yes, explain how.

Verdict mapping used in `evaluate.py`:

| Model says | Meaning | Ground-truth column |
|---|---|---|
| `Answer1 = "Yes"` | privacy policy **is** displayed → **COMPLIANT** | `compliance = Yes` |
| `Answer1 = "No"`  | no display found → **VIOLATION** | `compliance = No` |

The **baseline** always sees only the RA activity source file, in isolation. Each part below adds a
different retrieval mechanism on top of that same baseline analyst.

---

## Part A — `RA-ReAct/` : baseline vs RA-ReAct (multi-hop code following)

**RA-ReAct** sees the RA activity, and when the answer depends on a referenced helper/activity it
emits `Decision = CONTINUE` with a `next_target`; the harness retrieves that class's source and
appends it, then re-asks. This repeats (hops) until the model answers `YES` or hits a
`TERMINAL_DEADEND`.

| File | Model | App | Ground truth | Baseline | RA-ReAct |
|---|---|---|---|---|---|
| `01_gemini-2.5-flash_com.bosch.ebike.md`     | Gemini-2.5-flash | com.bosch.ebike (Bosch eBike Flow) | COMPLIANT | ❌ "No" | ✅ "Yes" |
| `02_gpt-5-mini_com.voltathletics.md`         | GPT-5-mini       | com.voltathletics.NativeApplication (Volt Athletics) | VIOLATION | ❌ "Yes" | ✅ "No" |
| `03_gpt-oss-120b_homeworkout.md`             | GPT-oss-120b     | homeworkout.homeworkouts.noequipment (Home Workout – No Equipment) | COMPLIANT | ❌ "No" | ✅ "Yes" |
| `04_qwen3-80b_com.imperon.android.gymapp.md` | Qwen3-80B        | com.imperon.android.gymapp (GymRun) | COMPLIANT | ❌ "No" | ✅ "Yes" |

- Cases 01, 03, 04 — the RA activity **delegates** the privacy-policy display to a helper method or
  another activity (`startActivity(...)`, `startCustomTab(...)`, or a static helper). Baseline, seeing
  only the opaque call, defaults to "No" (false-positive violation). RA-ReAct follows the reference,
  finds the code that actually loads the policy URL, and correctly returns "Yes".
- Case 02 — the reverse. Baseline sees a `HealthConnectPermissionDelegate` reference and
  **hallucinates** a display ("Yes"). RA-ReAct follows that class, finds only permission-request
  plumbing, and correctly returns "No" — removing the missed violation.

---

## Part B — `Baseline+RA-RAG/` : baseline vs Baseline + RA-RAG (retrieved reference)

**RA-RAG** = the `baseline-online` pipeline: the same single-pass baseline analyst, but the prompt is
augmented with a retrieved **"blunder book" reference** — a lesson distilled from a *past* mistaken
app, injected only when the current app's RA-source embedding is **cosine-similar (≥ threshold)** to
an app that lesson was drawn from. (`hc_baseline_online_*.py`; output under `output\baseline-online\`.)

These two examples are chosen so the correction is **provably** due to the retrieved reference: the
baseline prompt and the RA-RAG prompt are **identical except for the injected reference block** (same
model, same code, same settings) — a controlled A/B in which the answer flips wrong→right.

| File | Model | App | Ground truth | Baseline | Baseline + RA-RAG | Retrieved lesson |
|---|---|---|---|---|---|---|
| `05_RA-RAG_gemini-2.5-flash_digifit.virtuagym.md` | Gemini-2.5-flash | `digifit.virtuagym.client.android` (Virtuagym) | COMPLIANT | ❌ "No" | ✅ "Yes" | `[support: 4]` — "don't conclude 'No' from the absence of a direct code path" |
| `06_RA-RAG_llama-3.3-70b_com.loreal.smartoffice.md` | Llama-3.3-70B | `com.loreal.smartoffice` | VIOLATION | ❌ "Yes" | ✅ "No" | `[support: 24]` — "thoroughly examine what a web view actually displays; don't verdict from a surface pattern" |

- **Case 05 (Gemini):** baseline misses a Jetpack-Compose privacy-policy screen (no explicit
  `Intent`/`WebView`) → false-positive violation. The retrieved lesson — drawn from the SpotMe app and
  pulled in by embedding similarity — states the exact anti-pattern ("do not rely on the absence of a
  direct code trace"), and the analyst flips to the correct "Yes".
- **Case 06 (Llama):** baseline pattern-matches "`WebView` + `loadUrl`" to "compliant", even though the
  URL is Google's Health Connect *developer guide*, not a privacy policy → missed violation. The
  retrieved lesson pushes the analyst to judge *what the WebView actually displays*, flipping to "No".

---

Source data: `LLM_agentic_pipeline\<model>\output\{baseline,ReAct,baseline-online}\673_app_list\logs\<pkg>.log.json`,
ground truth: `LLM_agentic_pipeline\ground_truth.csv`.
