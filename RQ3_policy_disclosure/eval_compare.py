#!/usr/bin/env python3
"""
eval_compare.py - compare rq3_pp_label model output against the human `label`
ground truth in merged_admin_labels.json.

Alignment is per (app, permission). Ground-truth label prefixes (e.g. "Step")
are matched to permission names (e.g. "Steps") by normalization. Two metrics:

  Step 1 (disclosure): not_mentioned vs specific
  Step 2 (rationale) : rationale_yes vs rationale_no, on permissions both
                       sides call "specific" and where GT gives a definite
                       rationale label.

USAGE:
  python eval_compare.py --gt merged_admin_labels.json --pred output/gpt-oss-120b/llm_res.json
"""
import argparse, json, re, collections

SUFFIXES = ("not_mentioned", "rationale_yes", "rationale_no", "specific")


def norm(s):
    s = re.sub(r"[^a-z0-9]", "", str(s).lower())
    if s.endswith("s"):
        s = s[:-1]          # crude singularize: Steps->step
    return s


def split_label(lab):
    """Return (normalized_prefix, suffix) or (norm(lab), None) if no known suffix."""
    for suf in SUFFIXES:
        if lab.endswith("_" + suf) or lab == suf:
            prefix = lab[: -(len(suf) + 1)] if lab != suf else ""
            return norm(prefix), suf
    return norm(lab), None


def gt_decision(suffixes):
    """Collapse a permission's GT suffix set into (step1, rationale)."""
    s = set(suffixes)
    if s & {"specific", "rationale_yes", "rationale_no"}:
        step1 = "specific"
    elif "not_mentioned" in s:
        step1 = "not_mentioned"
    else:
        return ("other", None)
    if "rationale_yes" in s:
        rat = "yes"
    elif "rationale_no" in s:
        rat = "no"
    else:
        rat = None
    return (step1, rat)


def pred_decision(labels):
    s = [l for l in labels]
    has = lambda suf: any(l.endswith(suf) for l in s)
    if has("not_mentioned"):
        step1 = "not_mentioned"
    elif has("specific"):
        step1 = "specific"
    else:
        return ("other", None)
    if has("rationale_yes"):
        rat = "yes"
    elif has("rationale_no"):
        rat = "no"
    else:
        rat = None
    return (step1, rat)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt", required=True)
    ap.add_argument("--pred", required=True)
    ap.add_argument("--show", type=int, default=8, help="example disagreements to print")
    args = ap.parse_args()

    gt = json.load(open(args.gt, encoding="utf-8"))
    pred = json.load(open(args.pred, encoding="utf-8"))
    gt_by_pkg = {r["packagename"]: r for r in gt}

    n_pairs = 0
    unmatched_gt_labels = 0
    s1_total = 0; s1_correct = 0
    confusion = collections.Counter()      # (gt_step1, pred_step1)
    rat_total = 0; rat_correct = 0
    examples = []
    missing_pred_perm = 0
    pred_pkgs = 0

    for pr in pred:
        pkg = pr.get("packagename")
        if pkg not in gt_by_pkg:
            continue
        pred_pkgs += 1
        g = gt_by_pkg[pkg]
        perms = g.get("requested_permission") or []
        gt_labels = g.get("label") or []

        # assign GT labels to permissions by normalized prefix
        perm_norm = {norm(p): p for p in perms}
        gt_map = collections.defaultdict(list)
        for lab in gt_labels:
            pfx, suf = split_label(lab)
            # find the permission whose norm matches/contains this prefix
            target = None
            if pfx in perm_norm:
                target = perm_norm[pfx]
            else:
                for pn, p in perm_norm.items():
                    if pfx and (pfx.startswith(pn) or pn.startswith(pfx)):
                        target = p; break
            if target is None:
                unmatched_gt_labels += 1
            else:
                gt_map[target].append(suf if suf else "other")

        # model results by permission
        pred_map = {}
        if isinstance(pr.get("result"), list):
            for obj in pr["result"]:
                if isinstance(obj, dict) and "permission" in obj:
                    pred_map[obj["permission"]] = obj.get("labels") or []

        for p in perms:
            g_step1, g_rat = gt_decision(gt_map.get(p, []))
            if g_step1 == "other":
                continue                      # GT has no usable label for this perm
            if p not in pred_map:
                missing_pred_perm += 1
                continue
            p_step1, p_rat = pred_decision(pred_map[p])
            n_pairs += 1
            s1_total += 1
            confusion[(g_step1, p_step1)] += 1
            ok = (g_step1 == p_step1)
            if ok:
                s1_correct += 1
            # rationale metric
            if g_step1 == "specific" and p_step1 == "specific" and g_rat in ("yes", "no"):
                rat_total += 1
                if g_rat == p_rat:
                    rat_correct += 1
            if not ok and len(examples) < args.show:
                examples.append((pkg, p, g_step1, p_step1))

    print("=" * 60)
    print("Pred file : %s" % args.pred)
    print("Apps matched to GT     : %d" % pred_pkgs)
    print("Permissions evaluated  : %d" % s1_total)
    print("Unmatched GT labels    : %d  (prefix didn't map to a permission)"
          % unmatched_gt_labels)
    print("Perms missing in pred  : %d" % missing_pred_perm)
    print("-" * 60)
    print("STEP 1  (not_mentioned vs specific)")
    if s1_total:
        print("  Accuracy : %d/%d = %.1f%%" % (s1_correct, s1_total, 100*s1_correct/s1_total))
    print("  Confusion (GT -> Pred):")
    cats = ["not_mentioned", "specific", "other"]
    for gc in cats:
        row = {pc: confusion.get((gc, pc), 0) for pc in cats}
        if sum(row.values()):
            print("    GT %-14s -> %s" % (gc, row))
    print("-" * 60)
    print("STEP 2  (rationale yes/no, on agreed-specific w/ definite GT)")
    if rat_total:
        print("  Accuracy : %d/%d = %.1f%%" % (rat_correct, rat_total, 100*rat_correct/rat_total))
    else:
        print("  (no comparable rationale pairs)")
    if examples:
        print("-" * 60)
        print("Example Step-1 disagreements (pkg, perm, GT, Pred):")
        for pkg, p, g, pr_ in examples:
            print("  %-38s %-14s GT=%-13s Pred=%s" % (pkg, p, g, pr_))
    print("=" * 60)


if __name__ == "__main__":
    main()
