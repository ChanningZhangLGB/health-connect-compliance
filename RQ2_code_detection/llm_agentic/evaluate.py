import json, csv, os

# Positive class = VIOLATION (the thing we want to flag).
#   prediction "violation"  <=>  Answer1 == "No"   (no privacy-policy display => violation)
#   truth      "violation"  <=>  compliance == "No"
# So: TP = correctly flagged violation, FP = false alarm (flagged a compliant app),
#     FN = missed violation, TN = correctly cleared a compliant app.

BASE = os.environ.get("LLM_BASE", os.path.dirname(os.path.abspath(__file__)))

gt = {}
with open(BASE + r"\ground_truth.csv", encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        gt[row["package"].strip()] = row["compliance"].strip()

# (source_group, display_name, method_label, model_folder, pipeline_dir)
# pipeline_dir "baseline-online" = single-pass baseline analyst + online-learning curator
# (output of <model>\baseline_online_training\hc_baseline_online_*.py). Rows whose
# llm_res.json does not exist yet are reported as pending and skipped (so this script
# keeps working before those runs are launched).
MODELS = [
    ("Close source", "Gemini-2.5-flash", "baseline",    "gemini",                      "baseline"),
    ("Close source", "Gemini-2.5-flash", "ReAct",       "gemini",                      "ReAct"),
    ("Close source", "Gemini-2.5-flash", "base+online", "gemini",                      "baseline-online"),
    ("Close source", "GPT-5-mini",       "baseline",    "gpt-5-mini",                  "baseline"),
    ("Close source", "GPT-5-mini",       "ReAct",       "gpt-5-mini",                  "ReAct"),
    ("Close source", "GPT-5-mini",       "base+online", "gpt-5-mini",                  "baseline-online"),
    ("Close source", "Claude-Haiku-4.5", "baseline",    "claude-haiku-4-5",            "baseline"),
    ("Close source", "Claude-Haiku-4.5", "ReAct",       "claude-haiku-4-5",            "ReAct"),
    ("Close source", "Claude-Haiku-4.5", "base+online", "claude-haiku-4-5",            "baseline-online"),
    ("Open source",  "GPT-oss-120b",     "baseline",    "gpt-oss-120b",                "baseline"),
    ("Open source",  "GPT-oss-120b",     "ReAct",       "gpt-oss-120b",                "ReAct"),
    ("Open source",  "GPT-oss-120b",     "base+online", "gpt-oss-120b",                "baseline-online"),
    ("Open source",  "Llama-3.3-70b",    "baseline",    "llama",                       "baseline"),
    ("Open source",  "Llama-3.3-70b",    "ReAct",       "llama",                       "ReAct"),
    ("Open source",  "Llama-3.3-70b",    "base+online", "llama",                       "baseline-online"),
    ("Open source",  "Qwen3-80b",        "baseline",    "qwen3-next-80b-a3b-instruct", "baseline"),
    ("Open source",  "Qwen3-80b",        "ReAct",       "qwen3-next-80b-a3b-instruct", "ReAct"),
    ("Open source",  "Qwen3-80b",        "base+online", "qwen3-next-80b-a3b-instruct", "baseline-online"),
]


def metrics(folder, pipeline_dir):
    path = BASE + ("\\%s\\output\\%s\\673_app_list\\llm_res.json" % (folder, pipeline_dir))
    records = json.load(open(path, encoding="utf-8"))
    located = [r for r in records if r.get("locate_status") == "located"]

    tp = tn = fp = fn = 0
    for r in located:
        pkg = r.get("package") or r.get("fileName")
        pred_viol  = (r.get("Answer1") or "No").strip() == "No"
        truth_viol = gt.get(pkg, "No").strip() == "No"
        if   pred_viol and truth_viol:             tp += 1
        elif (not pred_viol) and (not truth_viol): tn += 1
        elif pred_viol and (not truth_viol):       fp += 1
        else:                                      fn += 1

    n    = tp + tn + fp + fn
    acc  = (tp + tn) / n if n else 0
    prec = tp / (tp + fp) if (tp + fp) else 0
    rec  = tp / (tp + fn) if (tp + fn) else 0
    f1   = 2 * prec * rec / (prec + rec) if (prec + rec) else 0
    return acc, prec, rec, f1, tp, tn, fp, fn, n


print(f"{'Model':<26}{'method':<13}{'Acc':>6}{'Prec':>7}{'Rec':>7}{'F1':>7}  | "
      f"{'TP':>4}{'TN':>5}{'FP':>5}{'FN':>5}{'N':>6}    (positive = VIOLATION)")
print("-" * 100)

out_rows = []
prev_group = prev_model = None
for group, name, method, folder, pdir in MODELS:
    try:
        acc, prec, rec, f1, tp, tn, fp, fn, n = metrics(folder, pdir)
    except FileNotFoundError:
        print(f"{name:<26}{method:<13}(no data yet - pipeline not run)")
        continue
    print(f"{name:<26}{method:<13}{acc:>6.3f}{prec:>7.3f}{rec:>7.3f}{f1:>7.3f}  | "
          f"{tp:>4}{tn:>5}{fp:>5}{fn:>5}{n:>6}")
    out_rows.append([
        group if group != prev_group else "",
        name  if name  != prev_model else "",
        method,
        round(acc, 4), round(prec, 4), round(rec, 4), round(f1, 4),
        tp, tn, fp, fn, n,
    ])
    prev_group, prev_model = group, name

OUT = BASE + r"\reporting\results_673_all_models.csv"
with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["Model", "", "method", "Acc", "Prec", "Rec", "F1", "TP", "TN", "FP", "FN", "N"])
    w.writerows(out_rows)
    w.writerow([])
    w.writerow(["", "violation = positive"])
    w.writerow(["", "compliance = negative"])

print(f"\nSaved to {OUT}")
