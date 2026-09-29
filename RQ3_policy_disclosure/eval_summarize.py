#!/usr/bin/env python3
"""
eval_summarize.py - compute Step-1 / Step-2 metrics for every model output under
output\\<model>\\llm_res.json vs the human `label` ground truth, and write a
summary CSV to result\\summary_metrics.csv.
"""
import json, re, csv, os, collections

# Repo-relative by default; override any of these with env vars.
BASE = os.path.dirname(os.path.abspath(__file__))
# ground truth: the `label` field in the admin-labels JSON (defaults to the
# in-repo 20-app sample; set RQ3_GT to your full merged_admin_labels.json).
GT_FILE = os.environ.get("RQ3_GT", os.path.join(BASE, "sample_data", "sample_admin_labels_20.json"))
OUT_ROOT = os.environ.get("RQ3_OUTPUT", os.path.join(BASE, "output"))
RESULT_DIR = os.environ.get("RQ3_RESULT", os.path.join(BASE, "result"))

SUFFIXES = ("not_mentioned", "rationale_yes", "rationale_no", "specific")


def norm(s):
    s = re.sub(r"[^a-z0-9]", "", str(s).lower())
    return s[:-1] if s.endswith("s") else s


def split_label(lab):
    for suf in SUFFIXES:
        if lab.endswith("_" + suf) or lab == suf:
            prefix = lab[: -(len(suf) + 1)] if lab != suf else ""
            return norm(prefix), suf
    return norm(lab), None


def gt_decision(suffixes):
    s = set(suffixes)
    if s & {"specific", "rationale_yes", "rationale_no"}:
        step1 = "specific"
    elif "not_mentioned" in s:
        step1 = "not_mentioned"
    else:
        return ("other", None)
    rat = "yes" if "rationale_yes" in s else ("no" if "rationale_no" in s else None)
    return (step1, rat)


def pred_decision(labels):
    has = lambda suf: any(l.endswith(suf) for l in labels)
    if has("not_mentioned"):
        step1 = "not_mentioned"
    elif has("specific"):
        step1 = "specific"
    else:
        return ("other", None)
    rat = "yes" if has("rationale_yes") else ("no" if has("rationale_no") else None)
    return (step1, rat)


def evaluate(gt, pred):
    gt_by_pkg = {r["packagename"]: r for r in gt}
    m = dict(apps=0, perms=0, s1_correct=0, tp=0, fp=0, tn=0, fn=0,
             rat_total=0, rat_correct=0, unmatched=0, missing=0,
             done=len(pred))
    for pr in pred:
        pkg = pr.get("packagename")
        if pkg not in gt_by_pkg:
            continue
        m["apps"] += 1
        g = gt_by_pkg[pkg]
        perms = g.get("requested_permission") or []
        perm_norm = {norm(p): p for p in perms}
        gt_map = collections.defaultdict(list)
        for lab in (g.get("label") or []):
            pfx, suf = split_label(lab)
            target = perm_norm.get(pfx)
            if target is None:
                for pn, p in perm_norm.items():
                    if pfx and (pfx.startswith(pn) or pn.startswith(pfx)):
                        target = p; break
            if target is None:
                m["unmatched"] += 1
            else:
                gt_map[target].append(suf if suf else "other")
        pred_map = {}
        if isinstance(pr.get("result"), list):
            for obj in pr["result"]:
                if isinstance(obj, dict) and "permission" in obj:
                    pred_map[obj["permission"]] = obj.get("labels") or []
        for p in perms:
            g1, grat = gt_decision(gt_map.get(p, []))
            if g1 == "other":
                continue
            if p not in pred_map:
                m["missing"] += 1
                continue
            p1, prat = pred_decision(pred_map[p])
            m["perms"] += 1
            if g1 == p1:
                m["s1_correct"] += 1
            if g1 == "specific" and p1 == "specific":
                m["tp"] += 1
            elif g1 == "not_mentioned" and p1 == "specific":
                m["fp"] += 1
            elif g1 == "not_mentioned" and p1 == "not_mentioned":
                m["tn"] += 1
            elif g1 == "specific" and p1 == "not_mentioned":
                m["fn"] += 1
            if g1 == "specific" and p1 == "specific" and grat in ("yes", "no"):
                m["rat_total"] += 1
                if grat == prat:
                    m["rat_correct"] += 1
    return m


def pct(a, b):
    return round(100.0 * a / b, 1) if b else ""


def main():
    gt = json.load(open(GT_FILE, encoding="utf-8"))
    os.makedirs(RESULT_DIR, exist_ok=True)
    rows = []
    for model in sorted(os.listdir(OUT_ROOT)):
        pred_file = os.path.join(OUT_ROOT, model, "llm_res.json")
        if not os.path.isfile(pred_file):
            continue
        pred = json.load(open(pred_file, encoding="utf-8"))
        m = evaluate(gt, pred)
        prec = pct(m["tp"], m["tp"] + m["fp"])
        rec = pct(m["tp"], m["tp"] + m["fn"])
        try:
            f1 = round(2 * prec * rec / (prec + rec), 1) if (prec and rec) else ""
        except Exception:
            f1 = ""
        rows.append({
            "model": model,
            "records_done": m["done"],
            "apps_evaluated": m["apps"],
            "perms_evaluated": m["perms"],
            "step1_accuracy_%": pct(m["s1_correct"], m["perms"]),
            "specific_precision_%": prec,
            "specific_recall_%": rec,
            "specific_f1": f1,
            "TP_specific": m["tp"], "FP": m["fp"], "TN_notmentioned": m["tn"], "FN_missed": m["fn"],
            "step2_rationale_accuracy_%": pct(m["rat_correct"], m["rat_total"]),
            "rationale_pairs": m["rat_total"],
            "unmatched_gt_labels": m["unmatched"],
            "perms_missing_in_pred": m["missing"],
        })
    out_csv = os.path.join(RESULT_DIR, "summary_metrics.csv")
    fields = list(rows[0].keys()) if rows else []
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print("Wrote %s  (%d model rows)" % (out_csv, len(rows)))
    for r in rows:
        print("  %-16s step1=%s%%  rationale=%s%%  (perms=%d)"
              % (r["model"], r["step1_accuracy_%"], r["step2_rationale_accuracy_%"], r["perms_evaluated"]))


if __name__ == "__main__":
    main()
