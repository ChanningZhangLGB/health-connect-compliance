#!/usr/bin/env python3
r"""
hc_multi_agent_haiku.py — code analyst + reflective practitioner (RQ2), online-continual.

A CODE ANALYST (follow-the-trail) reads the located RA, follows CONTINUE targets,
and co-determines a verdict, optionally guided by a BLUNDER BOOK injected into its
prompt. After each app is scored, its verdict is compared to ground truth: if
CORRECT, nothing happens (the book is unchanged). If WRONG, an LLM REFLECTIVE
PRACTITIONER reflects on [analyst OUTPUT + ground-truth label + existing book]
and either proposes a new lesson or attributes the case to an existing one. The
book therefore only grows from mistakes, which bounds its size.

Leak-free: an app is scored against the book built from PRIOR apps only, then (if
wrong) the practitioner sees its label.

The blunder book is JSON: a flat list of lessons, each = {insight, contributed_apps}.
Retrieval is threshold-gated over precomputed RA-source embeddings: a lesson is
injected when the current app's RA vector (looked up in ra_embeddings.csv by
package) has cosine similarity >= --sim-threshold (default 0.7) to ANY app that
contributed that lesson; otherwise the analyst runs memory-free. The pipeline only
READS the CSV; it does not embed at runtime.

Uses the Anthropic SDK (not OpenAI-compat). The analyst system prompt is passed
as the `system` parameter; the user message carries the per-app code + blunder book.

PREREQS: pip install anthropic ; set ANTHROPIC_API_KEY ; hc_extractor.py in ../../utils ;
         prompt files under prompt/ (prompt_code_analyst.txt, prompt_blunder_book.txt) ;
         ra_embeddings.csv (built once by build_ra_embeddings.py).
Online window: by default the practitioner only learns from the first --online-limit
apps; apps beyond that position are scored OFFLINE against the frozen book. Each
result records "online_phase": true/false. --online-limit -1 = learn on every app.

USAGE:
  python hc_multi_agent_haiku.py --input D:\..._java --applist D:\test20.txt --output D:\out
  python hc_multi_agent_haiku.py ... --sim-threshold 0.75
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "utils"))
import hc_extractor as hx

try:
    from anthropic import Anthropic
except ImportError:
    print("ERROR: pip install anthropic", file=sys.stderr)
    sys.exit(1)

MODEL    = "claude-haiku-4-5-20251001"
JAVA_LIMIT = 80000
MAX_HOPS = 8           # max CONTINUE follow steps before forcing a verdict

DEFAULT_LABELS = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "ground_truth_categorized.csv"))
LABEL_TEXT_EXCLUDE = {"located", "screenshot_path"}

SIM_THRESHOLD = 0.8  # cosine >= this to a blunder contributor -> inject that lesson

_HERE = os.path.dirname(os.path.abspath(__file__))
ANALYST_PROMPT_FILE = os.path.join(_HERE, "prompt", "prompt_code_analyst.txt")
PRACTITIONER_PROMPT_FILE = os.path.join(_HERE, "prompt", "prompt_blunder_book.txt")
RA_EMBEDDINGS_FILE = os.path.abspath(os.path.join(_HERE, "..", "..", "ra_embeddings.csv"))
GT_COMPLIANCE_FILE = os.path.abspath(os.path.join(_HERE, "..", "..", "ground_truth.csv"))

PRACTITIONER_LABEL_SECTION = """

-----
-----

## GROUND-TRUTH LABEL FOR THE CURRENT INPUT

[[GROUND_TRUTH_LABEL]]"""


def load_analyst_prompt(path):
    t = open(path, encoding="utf-8-sig").read().replace("\r\n", "\n")
    if "{memory}" not in t:
        marker = "------------ end of code ------------"
        idx = t.find(marker)
        if idx != -1:
            t = t[:idx + len(marker)] + "\n{memory}" + t[idx + len(marker):]
        else:
            t = t + "\n{memory}"
    si = t.find("Please analyze and answer")
    if si != -1:
        return t[si:].strip().replace("{{", "{").replace("}}", "}"), t[:si].rstrip() + "\n"
    return "", t


def load_practitioner_prompt(path):
    t = open(path, encoding="utf-8-sig").read().replace("\r\n", "\n")
    if "[[GROUND_TRUTH_LABEL]]" not in t:
        t = t + PRACTITIONER_LABEL_SECTION
    return "", t.strip()


ANALYST_SYSTEM, ANALYST_USER_TEMPLATE = load_analyst_prompt(ANALYST_PROMPT_FILE)
PRACTITIONER_SYSTEM, PRACTITIONER_USER_TEMPLATE = load_practitioner_prompt(PRACTITIONER_PROMPT_FILE)


def get_client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ERROR: set ANTHROPIC_API_KEY environment variable.", file=sys.stderr)
        sys.exit(1)
    return Anthropic(api_key=key)


def locate_ra_java(input_dir, package):
    app_root = hx.resolve_app_root(input_dir, package)
    sources_dir = os.path.join(app_root, "sources")
    manifest_path = os.path.join(app_root, "resources", "AndroidManifest.xml")

    info = {"status": "", "ra_class": "", "ra_style": "", "source_path": ""}
    if not os.path.isdir(app_root):
        info["status"] = "app_folder_missing"
        return info

    manifest = hx.parse_manifest(manifest_path, package)
    ra = manifest["rationale_activity"]
    info["ra_class"] = ra.get("implementation_class", "") or ""
    info["ra_style"] = ra.get("style", "") or ""
    if not ra.get("declared"):
        info["status"] = "no_ra_declared"
        return info

    for cand in (ra.get("candidate_classes") or [ra.get("implementation_class")]):
        sp, _method = hx.locate_ra_source(sources_dir, cand)
        if sp:
            info.update(ra_class=cand, source_path=sp, status="located")
            return info

    info["status"] = "source_not_found"
    return info


def read_ra_source(path, limit=JAVA_LIMIT):
    txt = open(path, encoding="utf-8", errors="replace").read()
    return txt[:limit] if len(txt) > limit else txt


def resolve_target(sources_dir, target):
    if not target:
        return (None, "empty_target")
    cls = target.strip().split("#")[0].split("(")[0].strip()
    return hx.locate_ra_source(sources_dir, cls)


def _format_block(label, path, code):
    return ("==== %s ====\n# file: %s\n%s" % (label, path, code))


PLACEHOLDER_APPS = {"com.example.health", "com.foo.tracker", "com.foo.bar",
                    "com.baz.qux", "com.example.app", "pkg1", "pkg2"}


def _empty_book():
    return []


def _book_empty(book):
    return not book


def _norm_book(book):
    out = []
    if not isinstance(book, list):
        return out
    for it in book:
        if not isinstance(it, dict):
            continue
        text = (it.get("insight") or "").strip()
        if not text:
            continue
        apps = it.get("contributed_apps") or []
        if isinstance(apps, str):
            apps = re.split(r"[,\s]+", apps)
        seen, uniq = set(), []
        for a in apps:
            a = str(a).strip()
            if a and a not in seen and a not in PLACEHOLDER_APPS:
                seen.add(a); uniq.append(a)
        if not uniq:
            continue
        out.append({"insight": text, "contributed_apps": uniq})
    return out


def _chunk_text(t, n):
    t = t or ""
    return [t[i:i + n] for i in range(0, len(t), n)]


def load_source_embeddings(path):
    import csv
    import numpy as np
    out = {}
    if not path or not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.reader(fh):
            if not row or row[0].strip().lower() == "package":
                continue
            try:
                v = np.asarray([float(x) for x in row[1:]], dtype="float32")
            except ValueError:
                continue
            n = float((v ** 2).sum()) ** 0.5
            out[row[0].strip()] = (v / n).astype("float32") if n else v
    return out


def _render_book(book, subset=None):
    src = subset if subset is not None else book
    if _book_empty(src):
        return "\n"
    lines = [
        "\n===================== BLUNDER BOOK (reference) =====================",
        "Below are lessons distilled from past apps where an analysis went WRONG. Each",
        "lesson is a pitfall to avoid, learned from a previous mistaken verdict. Use them",
        "to guide WHERE you look and HOW you weigh the evidence; they are guidance, not",
        "ground truth. Each lesson notes its support = the number of past apps it was drawn",
        "from (higher = more reliable). Always base your final verdict on the actual code",
        "shown to you.\n",
    ]
    for it in sorted(src, key=lambda x: -len(x.get("contributed_apps", []))):
        lines.append("[support: %d] %s\n"
                     % (len(it.get("contributed_apps", [])), it["insight"]))
    lines.append("=================== end of BLUNDER BOOK ===================\n")
    return "\n".join(lines)


class BlunderRetriever:
    def __init__(self, emb_path=None, threshold=0.7):
        self.threshold = threshold
        self._emb_map = load_source_embeddings(emb_path)
        self._book = _empty_book()
        self._bank_hash = None
        self.last_info = {"chars": 0, "packages": []}

    @property
    def has_embeddings(self):
        return bool(self._emb_map)

    def set_book(self, book):
        h = (hash(json.dumps(book, sort_keys=True, ensure_ascii=False)) if book else None)
        if h != self._bank_hash:
            self._bank_hash = h
            self._book = _norm_book(book)

    def _set_info(self, subset, rendered):
        apps = sorted({a for it in subset for a in it.get("contributed_apps", [])})
        self.last_info = {"chars": len(rendered) if rendered.strip() else 0, "packages": apps}

    def retrieve(self, package, contributors):
        if _book_empty(self._book):
            self.last_info = {"chars": 0, "packages": []}
            return "\n"
        q = self._emb_map.get(package)
        if q is None:
            self.last_info = {"chars": 0, "packages": []}
            return "\n"
        near = set()
        for p in (contributors or []):
            v = self._emb_map.get(p)
            if v is not None and p != package and float(v @ q) >= self.threshold:
                near.add(p)
        if not near:
            self.last_info = {"chars": 0, "packages": []}
            return "\n"
        subset = [it for it in self._book if set(it.get("contributed_apps", [])) & near]
        if not subset:
            self.last_info = {"chars": 0, "packages": []}
            return "\n"
        rendered = _render_book(self._book, subset=subset)
        self._set_info(subset, rendered)
        return rendered


def classify_text(client, code, memory_text="\n", log_sink=None, tag="",
                  max_retries=4, base_delay=2.0):
    # Split the user template at {code}/{memory} so the (intro + code) span becomes a
    # cacheable prefix. Across an app's multi-hop loop the code only grows at the tail,
    # so each hop reuses the previous hop's cached code via Anthropic prompt caching.
    # The blunder book (memory) is kept in a separate trailing block after the breakpoint.
    pre, _rest = ANALYST_USER_TEMPLATE.split("{code}", 1)
    mid, post = (_rest.split("{memory}", 1) + [""])[:2]
    code_prefix = pre + code + mid          # intro + code + end-of-code marker
    memory_tail = memory_text + post
    user_msg = code_prefix + memory_tail     # identical to the old single-string prompt
    user_content = [
        {"type": "text", "text": code_prefix, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": memory_tail},
    ]
    kwargs = {
        "model": MODEL, "max_tokens": 4096, "temperature": 0.0,
        "messages": [{"role": "user", "content": user_content}],
    }
    if ANALYST_SYSTEM:
        kwargs["system"] = [{"type": "text", "text": ANALYST_SYSTEM,
                             "cache_control": {"type": "ephemeral"}}]

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            msg = client.messages.create(**kwargs)
            content = "".join(b.text for b in msg.content if b.type == "text").strip()
            if log_sink is not None:
                u = getattr(msg, "usage", None)
                log_sink.append({
                    "tag": tag, "model": MODEL, "attempt": attempt,
                    "cache": {
                        "creation": getattr(u, "cache_creation_input_tokens", None),
                        "read": getattr(u, "cache_read_input_tokens", None),
                        "input": getattr(u, "input_tokens", None),
                    } if u else None,
                    "system": ANALYST_SYSTEM, "prompt": user_msg, "response": content,
                })
            return _parse_answer(content)
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                print("  [retry %d/%d after %.0fs] %s: %s"
                      % (attempt, max_retries, delay, tag, str(e)[:160]), file=sys.stderr)
                time.sleep(delay)
            elif log_sink is not None:
                log_sink.append({"tag": tag, "model": MODEL, "attempt": attempt,
                                 "error": str(e), "prompt": user_msg, "response": None})
    raise last_err


def _parse_answer(content):
    t = content.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
        t = t.strip()
    start = t.find("{")
    if start == -1:
        raise ValueError("no JSON object found in model response:\n%s" % content)
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(t[start:i + 1])
    raise ValueError("unbalanced JSON in model response:\n%s" % content)


def run_one(client, input_dir, package, retriever=None, contributors=None,
            bank_size=0, log_sink=None):
    loc = locate_ra_java(input_dir, package)
    rec = {
        "fileName": package, "package": package,
        "ra_class": loc["ra_class"], "ra_style": loc["ra_style"],
        "source_path": loc["source_path"], "locate_status": loc["status"],
        "Answer1": "No", "Answer2": "",
        "trail": [], "hops": 0, "stop_reason": "",
        "bank_size_at_scoring": bank_size,
    }
    if loc["status"] != "located":
        rec["note"] = "no RA source presented to model (%s)" % loc["status"]
        rec["stop_reason"] = "no_ra"
        return rec

    sources_dir = os.path.join(hx.resolve_app_root(input_dir, package), "sources")

    ra_code = read_ra_source(loc["source_path"])
    blocks = [_format_block("RATIONALE ACTIVITY: %s" % loc["ra_class"],
                            loc["source_path"], ra_code)]
    rec["trail"].append({"class": loc["ra_class"], "path": loc["source_path"]})
    visited_paths = {loc["source_path"]}

    memory_text = retriever.retrieve(package, contributors) if retriever else "\n"
    answer = {}
    for hop in range(MAX_HOPS + 1):
        rec["hops"] = hop
        combined = "\n\n".join(blocks)
        rec["combined_source"] = combined
        answer = classify_text(client, combined, memory_text=memory_text,
                               log_sink=log_sink, tag="%s#hop%d" % (package, hop))
        decision = (answer.get("Decision") or "").strip().upper()

        if decision == "YES" or answer.get("Answer1") == "Yes":
            rec.update(Answer1="Yes", Answer2=answer.get("Answer2", ""), stop_reason="yes")
            return rec

        if decision == "TERMINAL_DEADEND":
            rec.update(Answer1="No", Answer2=answer.get("Answer2", ""),
                       stop_reason="terminal_deadend")
            return rec

        if decision == "CONTINUE":
            if hop >= MAX_HOPS:
                rec.update(Answer1="No", Answer2=answer.get("Answer2", ""),
                           stop_reason="max_hops")
                return rec
            target = (answer.get("next_target") or "").strip()
            path, method = resolve_target(sources_dir, target)
            if not path or path in visited_paths:
                rec.update(Answer1="No", Answer2=answer.get("Answer2", ""),
                           stop_reason=("unresolved_target:%s" % target if not path
                                        else "revisit:%s" % target))
                rec["trail"].append({"class": target, "path": None, "resolve": method})
                return rec
            blocks.append(_format_block("FOLLOWED: %s" % target, path, read_ra_source(path)))
            visited_paths.add(path)
            rec["trail"].append({"class": target, "path": path, "resolve": method})
            continue

        rec.update(Answer1=answer.get("Answer1", "No"),
                   Answer2=answer.get("Answer2", ""), stop_reason="no_decision")
        return rec

    rec.update(Answer1=answer.get("Answer1", "No"), Answer2=answer.get("Answer2", ""),
               stop_reason=rec["stop_reason"] or "loop_end")
    return rec


# ---- blunder book bank (online-continual; practitioner-authored JSON memory base) ----

def load_bank(path):
    if os.path.isfile(path):
        try:
            b = json.load(open(path, encoding="utf-8"))
            b["blunder_book"] = _norm_book(b.get("blunder_book"))
            b.setdefault("version", 0)
            b.setdefault("contributors", [])
            return b
        except Exception:
            pass
    return {"blunder_book": _empty_book(), "version": 0, "contributors": []}


def save_bank(path, bank):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(bank, fh, indent=2, ensure_ascii=False)
    rendered = _render_book(bank.get("blunder_book") or _empty_book())
    if rendered.strip():
        with open(os.path.splitext(path)[0] + ".txt", "w", encoding="utf-8") as fh:
            fh.write(rendered)


def load_labels(csv_path):
    import csv
    out = {}
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            pkg = (row.get("package") or "").strip()
            if pkg:
                out[pkg] = {k: (v or "").strip() for k, v in row.items()
                            if k and k != "package"}
    return out


def format_label(label):
    if not label:
        return "(label unavailable)"
    lines = ["%s: %s" % (k, v) for k, v in label.items()
             if k not in LABEL_TEXT_EXCLUDE and v]
    return "\n".join(lines) if lines else "(label unavailable)"


def load_compliance(path):
    import csv
    out = {}
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                pkg = (row.get("package") or "").strip()
                if pkg:
                    out[pkg] = (row.get("compliance") or "").strip()
    except Exception as e:
        print("WARNING: could not load compliance for running accuracy (%s): %s"
              % (path, e), file=sys.stderr)
    return out


def running_accuracy(results, compliance):
    correct = scored = 0
    for r in results:
        if r.get("locate_status") != "located":
            continue
        pkg = r.get("package") or r.get("fileName")
        pred_viol  = (r.get("Answer1") or "No").strip() == "No"
        truth_viol = compliance.get(pkg, "No").strip() == "No"
        scored += 1
        if pred_viol == truth_viol:
            correct += 1
    acc = correct / scored if scored else 0.0
    return acc, correct, scored


def str2bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def find_screenshot(shots_dir, package):
    if not shots_dir:
        return None
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        p = os.path.join(shots_dir, package + ext)
        if os.path.isfile(p):
            return p
    return None


def _image_block(path):
    """Build an Anthropic base64 image content block."""
    mime, _ = mimetypes.guess_type(path)
    if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        mime = "image/png"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return {"type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64}}


def practitioner_reflect(client, examiner_rec, label, bank,
                         practitioner_model=MODEL, vision=True, screenshot_path=None,
                         log_sink=None, max_retries=4, base_delay=2.0):
    """claude-haiku-4-5 is vision-capable: when vision is on and a screenshot exists, the
    practitioner receives the app's UI screenshot alongside the text ground truth."""
    prev_book = bank.get("blunder_book") or _empty_book()
    book_text = (_render_book(prev_book) if not _book_empty(prev_book)
                 else "(empty — no blunder book yet)")
    model_answer = json.dumps({
        "Answer1": examiner_rec.get("Answer1"),
        "Answer2": examiner_rec.get("Answer2"),
        "Decision": examiner_rec.get("stop_reason"),
        "trail": [t.get("class") for t in examiner_rec.get("trail", [])],
    }, ensure_ascii=False, indent=2)

    user_text = (PRACTITIONER_USER_TEMPLATE
                 .replace("[[MODEL_ANSWER]]", model_answer)
                 .replace("[[GROUND_TRUTH_LABEL]]", format_label(label))
                 .replace("[[CASE_PACKAGE]]", examiner_rec.get("package") or "")
                 .replace("[[BLUNDER BOOK]]", book_text))

    used_image = None
    if vision and screenshot_path and os.path.isfile(screenshot_path):
        used_image = screenshot_path
        user_content = [
            {"type": "text", "text": user_text},
            _image_block(screenshot_path),
        ]
    else:
        user_content = user_text

    kwargs = {
        "model": practitioner_model, "max_tokens": 4096, "temperature": 0.0,
        "messages": [{"role": "user", "content": user_content}],
    }
    if PRACTITIONER_SYSTEM:
        kwargs["system"] = PRACTITIONER_SYSTEM

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            msg = client.messages.create(**kwargs)
            content = "".join(b.text for b in msg.content if b.type == "text").strip()
            if log_sink is not None:
                log_sink.append({
                    "practitioner": practitioner_model, "package": examiner_rec.get("package"),
                    "label": label, "examiner_verdict": examiner_rec.get("Answer1"),
                    "vision": bool(used_image), "image": used_image,
                    "system": PRACTITIONER_SYSTEM, "prompt": user_text, "response": content,
                })
            return _parse_answer(content)
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(base_delay * (2 ** (attempt - 1)))
            elif log_sink is not None:
                log_sink.append({"practitioner": practitioner_model,
                                 "package": examiner_rec.get("package"),
                                 "error": str(e), "prompt": user_text, "response": None})
    raise last_err


def apply_reflection(bank, reflection, package):
    book = bank["blunder_book"]
    has_new = (reflection.get("has_new_insight") or "").strip().lower().startswith("y")

    if has_new:
        text = (reflection.get("proposed_insight") or "").strip()
        if not text:
            return "no-op (empty proposed_insight)"
        for it in book:
            if it["insight"].strip() == text:
                if package not in it["contributed_apps"]:
                    it["contributed_apps"].append(package)
                return "merged into existing lesson"
        book.append({"insight": text, "contributed_apps": [package]})
        return "added new lesson"

    applied = (reflection.get("applied_principle") or "").strip()
    if not applied:
        return "no-op (No, but empty applied_principle)"
    hits = 0
    for it in book:
        ins = it["insight"].strip().lower()
        ap = applied.lower()
        if ins and (ins in ap or ap in ins):
            if package not in it["contributed_apps"]:
                it["contributed_apps"].append(package)
                hits += 1
    return ("attributed to %d existing lesson(s)" % hits if hits
            else "no-op (No match to an existing lesson)")


def main():
    ap = argparse.ArgumentParser(
        description="Multi-agent (RQ2): analyst + reflective practitioner, claude-haiku-4-5")
    ap.add_argument("--input", required=True, help="base dir containing <package>/ folders")
    ap.add_argument("--output", default="baseline_out", help="output dir")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--package", help="single package to analyze")
    grp.add_argument("--applist", help="text file: one package per line (batch)")
    ap.add_argument("--resume", action="store_true",
                    help="skip packages already present in the output JSON")
    ap.add_argument("--no-logs", action="store_true", help="do not write raw logs")
    ap.add_argument("--labels", default=DEFAULT_LABELS,
                    help="ground-truth CSV; pass '' to disable the practitioner")
    ap.add_argument("--ra-embeddings", default=RA_EMBEDDINGS_FILE,
                    help="precomputed RA-source embeddings CSV (build_ra_embeddings.py)")
    ap.add_argument("--sim-threshold", type=float, default=SIM_THRESHOLD,
                    help="cosine similarity >= this to a blunder contributor injects "
                         "that lesson (default 0.7)")
    ap.add_argument("--seed", type=int, default=42, help="seed for --shuffle ordering")
    ap.add_argument("--shuffle", type=str2bool, nargs="?", const=True, default=False,
                    help="shuffle app order with --seed (default: False)")
    ap.add_argument("--online-limit", type=int, default=-1,
                    help="cap online learning to first N apps; -1 = no limit (default)")
    ap.add_argument("--screenshots",
                    help="dir of UI screenshots <package>.png (fallback if labels lack "
                         "screenshot_path)")
    ap.add_argument("--practitioner-vision", type=str2bool, nargs="?", const=True, default=True,
                    help="send the app screenshot to the practitioner (default: True; this "
                         "model is vision-capable). Set False for a text-only ablation.")
    args = ap.parse_args()

    client = get_client()
    os.makedirs(args.output, exist_ok=True)
    out_file = os.path.join(args.output, "llm_res.json")
    bank_file = os.path.join(args.output, "blunder_book.json")
    logs_dir = os.path.join(args.output, "logs")
    if not args.no_logs:
        os.makedirs(logs_dir, exist_ok=True)

    if args.package:
        packages = [args.package]
    else:
        raw = open(args.applist, encoding="utf-8-sig").read()
        packages = [ln.strip().lstrip("﻿") for ln in raw.splitlines() if ln.strip()]

    if args.shuffle:
        import random
        random.Random(args.seed).shuffle(packages)

    curator_on = bool(args.labels) and os.path.isfile(args.labels)
    labels = load_labels(args.labels) if curator_on else {}
    if args.labels and not curator_on:
        print("WARNING: labels file not found, practitioner OFF: %s" % args.labels, file=sys.stderr)

    compliance = load_compliance(GT_COMPLIANCE_FILE)

    retriever = BlunderRetriever(args.ra_embeddings, args.sim_threshold)
    if not retriever.has_embeddings:
        print("WARNING: RA embeddings not found (%s); no memory will be retrieved. Build "
              "with build_ra_embeddings.py." % args.ra_embeddings, file=sys.stderr)

    bank = load_bank(bank_file)

    results, done = [], set()
    if args.resume and os.path.isfile(out_file):
        try:
            results = json.load(open(out_file, encoding="utf-8"))
            done = {r.get("package") or r.get("fileName") for r in results}
        except Exception:
            results, done = [], set()

    total = len(packages)
    print("Multi-agent pipeline (online-continual, blunder book): %d apps  ->  %s"
          % (total, args.output))
    online_desc = ("every app (no limit)" if args.online_limit < 0 else
                   "first %d apps (rest scored offline on frozen book)" % args.online_limit)
    print("Analyst: %s @ temp=0 | practitioner: %s (wrong-verdict only) | "
          "retrieval: cosine >= %.2f\nOrder: %s (seed=%d) | online learning: %s\n"
          % (MODEL, MODEL, args.sim_threshold,
             "shuffled" if args.shuffle else "as-listed", args.seed, online_desc))

    for i, pkg in enumerate(packages, 1):
        if args.resume and pkg in done:
            print("[%d/%d] %-45s SKIP (done)" % (i, total, pkg))
            continue

        log_sink = [] if not args.no_logs else None
        online = (args.online_limit < 0) or (i <= args.online_limit)

        retriever.set_book(bank.get("blunder_book"))
        retriever.last_info = {"chars": 0, "packages": []}
        mb_version = bank.get("version", 0)
        try:
            rec = run_one(client, args.input, pkg, retriever=retriever,
                          contributors=bank.get("contributors", []),
                          bank_size=mb_version, log_sink=log_sink)
            rec["online_phase"] = online
            combined_source = rec.pop("combined_source", "")
            results.append(rec)
            if rec["locate_status"] == "located":
                mi = retriever.last_info
                print("[%d/%d] %-45s OK  (Answer1=%s, book_v=%d)  retrieved=%dch from=[%s]"
                      % (i, total, pkg, rec["Answer1"], mb_version,
                         mi["chars"], ", ".join(mi["packages"])))
            else:
                print("[%d/%d] %-45s -- %s" % (i, total, pkg, rec["locate_status"]))
        except Exception as err:
            rec = {"fileName": pkg, "package": pkg, "locate_status": "error",
                   "error": str(err)[:300], "Answer1": "No", "Answer2": "",
                   "bank_size_at_scoring": mb_version, "online_phase": online}
            results.append(rec)
            print("[%d/%d] %-45s ERROR: %s" % (i, total, pkg, str(err)[:80]))

        with open(out_file, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, ensure_ascii=False)

        acc, ncorr, nsc = running_accuracy(results, compliance)
        print("        running acc : %.3f  (%d/%d located+scored)" % (acc, ncorr, nsc))

        if curator_on and online and rec.get("locate_status") == "located":
            label = labels.get(pkg)
            truth_viol = compliance.get(pkg, "No").strip() == "No"
            pred_viol = (rec.get("Answer1") or "No").strip() == "No"
            if pred_viol == truth_viol:
                print("        verdict correct -> proceed (book frozen at v%d)"
                      % bank.get("version", 0))
            else:
                shot = ((label or {}).get("screenshot_path")
                        or find_screenshot(args.screenshots, pkg))
                try:
                    refl = practitioner_reflect(client, rec, label, bank,
                                                vision=args.practitioner_vision,
                                                screenshot_path=shot, log_sink=log_sink)
                    status = apply_reflection(bank, refl, pkg)
                    if status.startswith("added") or status.startswith("merged"):
                        bank["version"] = bank.get("version", 0) + 1
                    if pkg not in bank["contributors"]:
                        bank["contributors"].append(pkg)
                    print("        WRONG verdict -> practitioner: %s (book v%d, %d lessons)"
                          % (status, bank["version"], len(bank["blunder_book"])))
                    save_bank(bank_file, bank)
                except Exception as err:
                    print("        practitioner FAILED (non-fatal): %s" % str(err)[:80],
                          file=sys.stderr)
        elif curator_on and not online and rec.get("locate_status") == "located":
            print("        offline: book frozen at v%d (no learning)"
                  % bank.get("version", 0))

        if log_sink is not None:
            with open(os.path.join(logs_dir, pkg + ".log.json"), "w", encoding="utf-8") as lf:
                json.dump(log_sink, lf, indent=2, ensure_ascii=False)

    n_yes = sum(1 for r in results if r.get("Answer1") == "Yes")
    n_loc = sum(1 for r in results if r.get("locate_status") == "located")
    print("\n===== MULTI-AGENT PIPELINE COMPLETE =====")
    print("Records         : %d" % len(results))
    print("RA located      : %d" % n_loc)
    print("Answer1 == Yes  : %d" % n_yes)
    _book = bank.get("blunder_book") or _empty_book()
    print("Blunder book    : v%d  (%d lessons, %d contributors)"
          % (bank.get("version", 0), len(_book), len(bank.get("contributors", []))))
    print("\nResults JSON    : %s" % out_file)
    print("Blunder book    : %s" % bank_file)
    if not args.no_logs:
        print("Raw logs        : %s" % logs_dir)


if __name__ == "__main__":
    main()
