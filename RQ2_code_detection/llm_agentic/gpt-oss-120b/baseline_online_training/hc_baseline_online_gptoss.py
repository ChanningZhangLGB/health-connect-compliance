#!/usr/bin/env python3
r"""
hc_baseline_online_gptoss.py — single-pass baseline analyst + reflective practitioner
(RQ2), online-continual. Baseline counterpart of the ReAct multi-agent pipeline:
identical online-learning curator, but the analyst is the SINGLE-PASS baseline (one
LLM call on ONE located RA source — no follow-the-trail hops).

A BASELINE ANALYST reads the located RA and makes ONE verdict call, optionally guided
by a BLUNDER BOOK injected into its prompt. After each app is scored, its verdict is
compared to ground truth: if
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

The analyst sends ONE user message (per-app code + retrieved book + the two baseline
questions — no system/user split); the practitioner prompt is a single USER message.

PREREQS: pip install openai ; set NVIDIA_API_KEY ; hc_extractor.py in ../../utils ;
         prompt files under prompt/ (prompt_baseline_analyst.txt, prompt_blunder_book.txt) ;
         ra_embeddings.csv (built once by build_ra_embeddings.py).
Online window: by default the practitioner only learns from the first --online-limit
apps; apps beyond that position are scored OFFLINE against the frozen book. Each
result records "online_phase": true/false. --online-limit -1 = learn on every app.

USAGE:
  python hc_baseline_online_gptoss.py --input D:\..._java --applist D:\test20.txt --output D:\out
  python hc_baseline_online_gptoss.py ... --sim-threshold 0.75
  python hc_baseline_online_gptoss.py ... --practitioner-vision --practitioner-model <vision-model>
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
    from openai import OpenAI
except ImportError:
    print("ERROR: pip install openai", file=sys.stderr)
    sys.exit(1)

MODEL    = "openai/gpt-oss-120b"
BASE_URL = "https://integrate.api.nvidia.com/v1"
JAVA_LIMIT = 80000

DEFAULT_LABELS = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "ground_truth_categorized.csv"))
# Label columns not rendered as text (located = bookkeeping; screenshot_path feeds vision).
LABEL_TEXT_EXCLUDE = {"located", "screenshot_path"}

SIM_THRESHOLD = 0.8  # cosine >= this to a blunder contributor -> inject that lesson

_HERE = os.path.dirname(os.path.abspath(__file__))
ANALYST_PROMPT_FILE = os.path.join(_HERE, "prompt", "prompt_baseline_analyst.txt")
PRACTITIONER_PROMPT_FILE = os.path.join(_HERE, "prompt", "prompt_blunder_book.txt")
# Precomputed RA-source embeddings (project root; build with build_ra_embeddings.py).
RA_EMBEDDINGS_FILE = os.path.abspath(os.path.join(_HERE, "..", "..", "ra_embeddings.csv"))
# Ground-truth compliance for the live running-accuracy readout. Loaded independently
# of --labels (so the inline number shows even with the curator off) and uses the same
# source + convention as evaluate.py, so the running accuracy matches the final report.
GT_COMPLIANCE_FILE = os.path.abspath(os.path.join(_HERE, "..", "..", "ground_truth.csv"))

# Slots appended to the practitioner prompt if its file lacks them.
PRACTITIONER_LABEL_SECTION = """

-----
-----

## GROUND-TRUTH LABEL FOR THE CURRENT INPUT

[[GROUND_TRUTH_LABEL]]"""


def load_analyst_prompt(path):
    """Single-pass baseline analyst: the whole prompt (intro + code + {memory} + the
    two questions + JSON spec) is ONE user message — no system/user split. Returns
    ("", user_template) so the shared classify_text sends a single user message.
    {memory} is injected after the end-of-code marker if the file lacks it; the {{ }}
    JSON-brace escapes are kept for str.format()."""
    t = open(path, encoding="utf-8-sig").read().replace("\r\n", "\n")
    if "{memory}" not in t:
        marker = "------------ end of code ------------"
        idx = t.find(marker)
        if idx != -1:
            t = t[:idx + len(marker)] + "\n{memory}" + t[idx + len(marker):]
        else:
            t = t + "\n{memory}"
    return "", t


def load_practitioner_prompt(path):
    """Split the practitioner prompt into (system, user_template). The user part is
    everything from the analysis OUTPUT onward (the dynamic [[...]] sections); any
    preamble before the first '------------ start of output' marker is treated as
    system. Appends a ground-truth label slot if missing."""
    t = open(path, encoding="utf-8-sig").read().replace("\r\n", "\n")
    if "[[GROUND_TRUTH_LABEL]]" not in t:
        t = t + PRACTITIONER_LABEL_SECTION
    # the draft has no separate system preamble; keep it all as the user template
    return "", t.strip()


ANALYST_SYSTEM, ANALYST_USER_TEMPLATE = load_analyst_prompt(ANALYST_PROMPT_FILE)
PRACTITIONER_SYSTEM, PRACTITIONER_USER_TEMPLATE = load_practitioner_prompt(PRACTITIONER_PROMPT_FILE)


def get_client():
    key = os.environ.get("NVIDIA_API_KEY")
    if not key:
        print("ERROR: set NVIDIA_API_KEY environment variable.", file=sys.stderr)
        sys.exit(1)
    return OpenAI(base_url=BASE_URL, api_key=key)


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


# ---- blunder book: JSON, a flat list of lessons learned from WRONG verdicts ----
# Each item = {"insight": str, "contributed_apps": [pkg, ...]}. The book only grows
# when the analyst's verdict disagrees with ground truth (a "blunder"); correct
# verdicts add nothing. Retrieval injects a lesson when the current app is similar
# (cosine >= threshold) to any app that contributed that lesson.
PLACEHOLDER_APPS = {"com.example.health", "com.foo.tracker", "com.foo.bar",
                    "com.baz.qux", "com.example.app", "pkg1", "pkg2"}


def _empty_book():
    return []


def _book_empty(book):
    return not book


def _norm_book(book):
    """Coerce a parsed blunder book into the canonical list shape; drop malformed
    items and any item left with no real provenance (placeholder/example apps)."""
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


def embed_text(model, text):
    """SBERT embed with chunked mean-pooling -> one unit-normalized 384-d vector
    (chunking represents the full source despite SBERT's ~256-token truncation).
    Shared with build_ra_embeddings.py so indexed and query vectors match."""
    import numpy as np
    chunks = _chunk_text(text, 1000) or [""]
    vecs = np.asarray(model.encode(chunks, normalize_embeddings=True), dtype="float32")
    v = vecs.mean(axis=0)
    n = float((v ** 2).sum()) ** 0.5
    return (v / n).astype("float32") if n else v.astype("float32")


def load_source_embeddings(path):
    """Load CSV 'package,e0..eD' -> {pkg: unit-normalized vec}."""
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
    """Render the blunder book into the analyst-facing block. `subset` (a list of
    items) renders only the retrieved lessons; default = all. contributed_apps are
    NOT shown (provenance/retrieval only) — only the support count; lessons are
    ordered by support, strongest first."""
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
    """Threshold-gated retrieval over precomputed RA-source embeddings. For the
    current app, a lesson is injected if ANY of its contributed_apps has cosine
    similarity >= threshold to the current app's RA vector. No top-k cap. If the
    current app has no embedding, or no lesson clears the threshold, nothing is
    injected (the analyst runs memory-free)."""

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
        h = (hash(json.dumps(book, sort_keys=True, ensure_ascii=False))
             if book else None)
        if h != self._bank_hash:
            self._bank_hash = h
            self._book = _norm_book(book)

    def _set_info(self, subset, rendered):
        apps = sorted({a for it in subset for a in it.get("contributed_apps", [])})
        self.last_info = {"chars": len(rendered) if rendered.strip() else 0,
                          "packages": apps}

    def retrieve(self, package, contributors):
        """Memory text for the current app. contributors = packages already in the
        book (leak-free candidate set). A lesson is selected iff one of its
        contributed_apps is >= threshold similar to the current app."""
        if _book_empty(self._book):
            self.last_info = {"chars": 0, "packages": []}
            return "\n"
        q = self._emb_map.get(package)
        if q is None:                          # current app not embedded -> no memory
            self.last_info = {"chars": 0, "packages": []}
            return "\n"
        # apps that clear the similarity threshold to the current app
        near = set()
        for p in (contributors or []):
            v = self._emb_map.get(p)
            if v is not None and p != package and float(v @ q) >= self.threshold:
                near.add(p)
        if not near:
            self.last_info = {"chars": 0, "packages": []}
            return "\n"
        subset = [it for it in self._book
                  if set(it.get("contributed_apps", [])) & near]
        if not subset:
            self.last_info = {"chars": 0, "packages": []}
            return "\n"
        rendered = _render_book(self._book, subset=subset)
        self._set_info(subset, rendered)
        return rendered


def classify_text(client, code, memory_text="\n", log_sink=None, tag="",
                  max_retries=4, base_delay=2.0):
    user_msg = ANALYST_USER_TEMPLATE.format(code=code, memory=memory_text)
    messages = ([{"role": "system", "content": ANALYST_SYSTEM}] if ANALYST_SYSTEM else [])
    messages.append({"role": "user", "content": user_msg})

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            completion = client.chat.completions.create(
                model=MODEL, messages=messages, temperature=0.0,
                top_p=1, max_tokens=4096, stream=False,
            )
            content = (completion.choices[0].message.content or "").strip()
            if log_sink is not None:
                log_sink.append({
                    "tag": tag, "model": MODEL, "attempt": attempt,
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
    """Single-pass baseline analyst: locate the RA, read ONE source file, and make
    ONE classification call — optionally with the retrieved blunder book injected.
    No follow-the-trail hops; this is the baseline analyst under online learning."""
    loc = locate_ra_java(input_dir, package)
    rec = {
        "fileName": package, "package": package,
        "ra_class": loc["ra_class"], "ra_style": loc["ra_style"],
        "source_path": loc["source_path"], "locate_status": loc["status"],
        "Answer1": "No", "Answer2": "",
        "bank_size_at_scoring": bank_size,
    }
    if loc["status"] != "located":
        rec["note"] = "no RA source presented to model (%s)" % loc["status"]
        return rec

    code = read_ra_source(loc["source_path"])
    memory_text = retriever.retrieve(package, contributors) if retriever else "\n"
    answer = classify_text(client, code, memory_text=memory_text,
                           log_sink=log_sink, tag=package)
    rec["Answer1"] = answer.get("Answer1", "No")
    rec["Answer2"] = answer.get("Answer2", "")
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
    rendered = _render_book(bank.get("blunder_book") or _empty_book())  # readable sidecar
    if rendered.strip():
        with open(os.path.splitext(path)[0] + ".txt", "w", encoding="utf-8") as fh:
            fh.write(rendered)


def load_labels(csv_path):
    """package,... CSV -> {package: {other columns}}."""
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
    """Render every semantic label layer for the curator, omitting bookkeeping
    columns (located / screenshot_path)."""
    if not label:
        return "(label unavailable)"
    lines = ["%s: %s" % (k, v) for k, v in label.items()
             if k not in LABEL_TEXT_EXCLUDE and v]
    return "\n".join(lines) if lines else "(label unavailable)"


def find_screenshot(shots_dir, package):
    if not shots_dir:
        return None
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        p = os.path.join(shots_dir, package + ext)
        if os.path.isfile(p):
            return p
    return None


def load_compliance(path):
    """package -> compliance ('Yes'/'No') from ground_truth.csv (matches evaluate.py).
    Independent of the curator's --labels file so the running accuracy always shows."""
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
    """Running accuracy over located+scored records so far, mirroring evaluate.py:
    pred violation = Answer1=='No'; truth violation = compliance=='No' (default 'No');
    only locate_status=='located' records count. Returns (accuracy, n_correct, n_scored)."""
    correct = scored = 0
    for r in results:
        if r.get("locate_status") != "located":
            continue
        pkg = r.get("package") or r.get("fileName")
        pred_viol  = (r.get("Answer1") or "No").strip() == "No"
        truth_viol = compliance.get(pkg, "No").strip() == "No"
        scored += 1
        if pred_viol == truth_viol:          # TP or TN
            correct += 1
    acc = correct / scored if scored else 0.0
    return acc, correct, scored


def str2bool(v):
    """Parse a boolean CLI value. Accepts 1/true/yes/y/on (any case) as True."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _image_data_url(path):
    mime, _ = mimetypes.guess_type(path)
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return "data:%s;base64,%s" % (mime or "image/png", b64)


def practitioner_reflect(client, examiner_rec, label, bank,
                         practitioner_model=MODEL, vision=False, screenshot_path=None,
                         log_sink=None, max_retries=4, base_delay=2.0):
    """Run the reflective practitioner on a WRONG-verdict case. It reads the analyst's
    OUTPUT (verdict / explanation / trail), the ground-truth label, and the existing
    blunder book, and returns a dict:
        {has_new_insight: 'Yes'/'No', applied_principle: str, proposed_insight: str}
    If vision and a screenshot exist, attach it (practitioner_model must be vision-capable)."""
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
            {"type": "image_url", "image_url": {"url": _image_data_url(screenshot_path)}},
        ]
    else:
        user_content = user_text

    messages = ([{"role": "system", "content": PRACTITIONER_SYSTEM}] if PRACTITIONER_SYSTEM else [])
    messages.append({"role": "user", "content": user_content})

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            completion = client.chat.completions.create(
                model=practitioner_model, messages=messages,
                temperature=0.0, top_p=1, max_tokens=4096, stream=False,
            )
            content = (completion.choices[0].message.content or "").strip()
            if log_sink is not None:
                log_sink.append({
                    "practitioner": practitioner_model, "package": examiner_rec.get("package"),
                    "label": label, "examiner_verdict": examiner_rec.get("Answer1"),
                    "vision": bool(used_image), "image": used_image,
                    "system": PRACTITIONER_SYSTEM, "prompt": user_text, "response": content,
                })
            return _parse_answer(content)            # reuse the brace-matched JSON parser
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
    """Mutate the blunder book per the practitioner's reflection. Returns a short
    status string for logging. 'Yes' -> append proposed_insight as a new lesson
    (contributor = this app). 'No' -> add this app to the contributors of the
    lesson(s) it applied (best-effort substring match on applied_principle)."""
    book = bank["blunder_book"]
    has_new = (reflection.get("has_new_insight") or "").strip().lower().startswith("y")

    if has_new:
        text = (reflection.get("proposed_insight") or "").strip()
        if not text:
            return "no-op (empty proposed_insight)"
        for it in book:                         # dedup exact text -> just add contributor
            if it["insight"].strip() == text:
                if package not in it["contributed_apps"]:
                    it["contributed_apps"].append(package)
                return "merged into existing lesson"
        book.append({"insight": text, "contributed_apps": [package]})
        return "added new lesson"

    # has_new == No: attribute this app to the applied principle(s)
    applied = (reflection.get("applied_principle") or "").strip()
    if not applied:
        return "no-op (No, but empty applied_principle)"
    hits = 0
    for it in book:
        ins = it["insight"].strip().lower()
        ap = applied.lower()
        if ins and (ins in ap or ap in ins):    # loose match either direction
            if package not in it["contributed_apps"]:
                it["contributed_apps"].append(package)
                hits += 1
    return ("attributed to %d existing lesson(s)" % hits if hits
            else "no-op (No match to an existing lesson)")


def main():
    ap = argparse.ArgumentParser(
        description="Baseline + online learning (RQ2): single-pass analyst + memory curator, gpt-oss-120b")
    ap.add_argument("--input", required=True, help="base dir containing <package>/ folders")
    ap.add_argument("--output", default="baseline_out", help="output dir")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--package", help="single package to analyze")
    grp.add_argument("--applist", help="text file: one package per line (batch)")
    ap.add_argument("--resume", action="store_true",
                    help="skip packages already present in the output JSON")
    ap.add_argument("--no-logs", action="store_true", help="do not write raw logs")
    ap.add_argument("--compliance", default=GT_COMPLIANCE_FILE,
                    help="ground-truth compliance CSV (package,compliance) driving the "
                         "curator's right/wrong detection and running accuracy "
                         "(default: project-root ground_truth.csv)")
    ap.add_argument("--labels", default=DEFAULT_LABELS,
                    help="ground-truth CSV (default: ground_truth_categorized.csv); "
                         "pass '' to disable the curator")
    ap.add_argument("--screenshots",
                    help="dir of UI screenshots <package>.png (fallback if labels lacks it)")
    ap.add_argument("--ra-embeddings", default=RA_EMBEDDINGS_FILE,
                    help="precomputed RA-source embeddings CSV (build_ra_embeddings.py)")
    ap.add_argument("--sim-threshold", type=float, default=SIM_THRESHOLD,
                    help="cosine similarity >= this to a blunder contributor injects "
                         "that lesson (default 0.7)")
    ap.add_argument("--practitioner-vision", action="store_true",
                    help="send the app screenshot to the practitioner (needs a vision model)")
    ap.add_argument("--practitioner-model", default=None,
                    help="practitioner model (default: analyst model; set a vision model "
                         "with --practitioner-vision)")
    ap.add_argument("--seed", type=int, default=42, help="seed for --shuffle ordering")
    ap.add_argument("--shuffle", type=str2bool, nargs="?", const=True, default=False,
                    help="shuffle app order with --seed (default: False). Pass "
                         "'--shuffle' or '--shuffle true' to enable; order is a "
                         "variable in the online-continual setup.")
    ap.add_argument("--online-limit", type=int, default=-1,
                    help="cap the number of apps for which the curator performs ONLINE "
                         "learning. Apps beyond this position are scored OFFLINE against "
                         "the frozen accumulated memory (curator skipped). "
                         "Default: -1 = learn on every app (no offline phase). "
                         "Pass a positive integer (e.g. 50/100/150/200) to freeze the "
                         "book after that many apps and score the rest offline.")
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

    if args.shuffle:                       # ordering IS the experiment here
        import random
        random.Random(args.seed).shuffle(packages)

    curator_on = bool(args.labels) and os.path.isfile(args.labels)
    labels = load_labels(args.labels) if curator_on else {}
    if args.labels and not curator_on:
        print("WARNING: labels file not found, curator OFF: %s" % args.labels, file=sys.stderr)

    compliance = load_compliance(args.compliance)   # for the live running-accuracy readout

    practitioner_model = args.practitioner_model or MODEL
    if args.practitioner_vision and not args.practitioner_model:
        print("WARNING: --practitioner-vision set but practitioner model is the text-only "
              "analyst model; images will likely be rejected.", file=sys.stderr)

    retriever = BlunderRetriever(args.ra_embeddings, args.sim_threshold)
    if not retriever.has_embeddings:
        print("WARNING: RA embeddings not found (%s); no memory will be retrieved. Build "
              "with build_ra_embeddings.py." % args.ra_embeddings, file=sys.stderr)

    bank = load_bank(bank_file)            # reloaded on resume (order must match)

    results, done = [], set()
    if args.resume and os.path.isfile(out_file):
        try:
            results = json.load(open(out_file, encoding="utf-8"))
            done = {r.get("package") or r.get("fileName") for r in results}
        except Exception:
            results, done = [], set()

    total = len(packages)
    print("Baseline + online-learning pipeline (single-pass analyst, blunder book): %d apps  ->  %s"
          % (total, args.output))
    online_desc = ("every app (no limit)" if args.online_limit < 0 else
                   "first %d apps (rest scored offline on frozen book)" % args.online_limit)
    print("Analyst: %s @ temp=0 | practitioner: %s%s (wrong-verdict only) | "
          "retrieval: cosine >= %.2f\nOrder: %s (seed=%d) | online learning: %s\n"
          % (MODEL, practitioner_model,
             ", vision" if args.practitioner_vision else "",
             args.sim_threshold,
             "shuffled" if args.shuffle else "as-listed", args.seed, online_desc))

    for i, pkg in enumerate(packages, 1):
        if args.resume and pkg in done:
            print("[%d/%d] %-45s SKIP (done)" % (i, total, pkg))
            continue

        log_sink = [] if not args.no_logs else None

        # ONLINE window: curate (grow memory) only for the first N seen apps; beyond that
        # the bank is frozen and apps are scored OFFLINE against the accumulated memory.
        online = (args.online_limit < 0) or (i <= args.online_limit)

        # 1) ANALYST — scored against the book as it is RIGHT NOW (prior apps only).
        retriever.set_book(bank.get("blunder_book"))
        retriever.last_info = {"chars": 0, "packages": []}
        mb_version = bank.get("version", 0)
        try:
            rec = run_one(client, args.input, pkg, retriever=retriever,
                          contributors=bank.get("contributors", []),
                          bank_size=mb_version, log_sink=log_sink)
            rec["online_phase"] = online
            combined_source = rec.pop("combined_source", "")   # not stored in results
            results.append(rec)
            if rec["locate_status"] == "located":
                mi = retriever.last_info                        # retrieved-memory summary
                print("[%d/%d] %-45s OK  (Answer1=%s, book_v=%d)  retrieved=%dch from=[%s]"
                      % (i, total, pkg, rec["Answer1"], mb_version,
                         mi["chars"], ", ".join(mi["packages"])))
            else:
                print("[%d/%d] %-45s -- %s" % (i, total, pkg, rec["locate_status"]))
        except Exception as err:
            rec = {"fileName": pkg, "package": pkg, "locate_status": "error",
                   "error": str(err)[:300], "Answer1": "No", "Answer2": "",
                   "bank_size_at_scoring": mb_version, "online_phase": online}
            combined_source = ""
            results.append(rec)
            print("[%d/%d] %-45s ERROR: %s" % (i, total, pkg, str(err)[:80]))

        with open(out_file, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, ensure_ascii=False)

        # running accuracy so far (located+scored only; same convention as evaluate.py)
        acc, ncorr, nsc = running_accuracy(results, compliance)
        print("        running acc : %.3f  (%d/%d located+scored)" % (acc, ncorr, nsc))

        # 2) VERDICT == GROUND TRUTH?  Correct -> proceed (book unchanged). Wrong ->
        # the reflective practitioner learns a lesson. Only within the online window;
        # afterwards the book is frozen (offline scoring). Runs after scoring, so the
        # label can't affect this app's recorded verdict.
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
                                                practitioner_model=practitioner_model,
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
    print("\n===== BASELINE + ONLINE-LEARNING PIPELINE COMPLETE =====")
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
