#!/usr/bin/env python3
"""
hc_ReAct_qwen.py — single-agent + follow-the-trail (RQ2).

Baseline = one LLM call on the located RA. This variant adds ONE thing: when the
located RA alone is inconclusive, the model does not jump straight to "No". It
first decides whether to CONTINUE (name a next_target class to examine) or call
TERMINAL_DEADEND. On CONTINUE the script resolves that class with hc_extractor,
appends its source, and re-examines — looping until a Yes, a dead-end, an
unresolvable target, or the hop guard. The final verdict is co-determined over
all source gathered along the trail.

Everything else (RA location, CLI, output shape, parsing, NVIDIA config) is
identical to the baseline so results stay comparable.

Model: qwen/qwen3-next-80b-a3b-instruct on NVIDIA, temperature=0.

PREREQS: pip install openai ; set NVIDIA_API_KEY ; hc_extractor.py in ../../utils.

USAGE:
  python hc_ReAct_qwen.py --input D:\\android_studio_apk_all_decompile_java --applist D:\\test20.txt --output D:\\followup_out
  python hc_ReAct_qwen.py --input D:\\android_studio_apk_all_decompile_java --package com.foo --output D:\\followup_out
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "utils"))
import hc_extractor as hx

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: pip install openai", file=sys.stderr)
    sys.exit(1)

MODEL    = "qwen/qwen3-next-80b-a3b-instruct"
BASE_URL = "https://integrate.api.nvidia.com/v1"
SEED     = 42
JAVA_LIMIT = 80000
MAX_HOPS = 8           # max CONTINUE follow steps before forcing a verdict

# Prompt — baseline questions + follow-the-trail decision.
PROMPT_TEMPLATE = """
You are given the contents of a Java source file below.

------------ start of code ------------
{code}
------------ end of code ------------

Please analyze and answer these two questions:
1. Does this code implement an activity that displays app's privacy policy when the user clicks on the privacy policy link in the Health Connect permissions screen? Answer1:[Yes/No]
2. If yes, explain the code that implements the activity. Answer2:[Explanation] 

Before you answer "No", consider that the code you can see so far may delegate
the privacy-policy display to ANOTHER class (e.g. it starts another Activity,
calls a helper/handler, or references a class that actually loads the policy).
So when the code shown does NOT by itself implement the privacy-policy display,
do not immediately conclude "No". Instead decide between exactly these cases:

  - CONTINUE        : The examination should follow to another class to decide.
                      Provide "next_target" = the fully-qualified (or as
                      complete as possible) name of the single most likely next
                      class to examine.
  - TERMINAL_DEADEND: There is no privacy-policy display here and nothing to
                      follow (no further class is referenced that could plausibly
                      implement it). This means the final answer is "No".

If, considering ALL the code shown to you (which may span several files), the
privacy-policy display IS implemented, answer "Yes".

respond *only* with a JSON object with keys:
  - "Answer1": "Yes" or "No"
  - "Answer2": a brief explanation (or empty string if Answer1 is "No")
  - "Decision": one of "YES", "CONTINUE", "TERMINAL_DEADEND"
       * use "YES"  when Answer1 is "Yes"
       * use "CONTINUE" when you need to follow to another class
       * use "TERMINAL_DEADEND" when Answer1 is "No" and nothing is left to follow
  - "next_target": the class name to examine next when Decision is "CONTINUE",
                   otherwise an empty string

Example when the policy display is implemented here:

{{
  "Answer1": "Yes",
  "Answer2": "This activity registers a click listener on the privacy-policy link...",
  "Decision": "YES",
  "next_target": ""
}}

Example when you must follow to another class:

{{
  "Answer1": "No",
  "Answer2": "",
  "Decision": "CONTINUE",
  "next_target": "com.example.privacy.PrivacyPolicyActivity"
}}

Example when there is nothing here and nothing to follow:

{{
  "Answer1": "No",
  "Answer2": "",
  "Decision": "TERMINAL_DEADEND",
  "next_target": ""
}}
"""


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

    cands = ra.get("candidate_classes") or [ra.get("implementation_class")]
    for cand in cands:
        sp, _method = hx.locate_ra_source(sources_dir, cand)
        if sp:
            info["ra_class"] = cand
            info["source_path"] = sp
            info["status"] = "located"
            return info

    info["status"] = "source_not_found"
    return info


def read_ra_source(path, limit=JAVA_LIMIT):
    txt = open(path, encoding="utf-8", errors="replace").read()
    return txt[:limit] if len(txt) > limit else txt


def resolve_target(sources_dir, target):
    """Resolve next_target to a .java via hc_extractor.locate_ra_source."""
    if not target:
        return (None, "empty_target")
    cls = target.strip().split("#")[0].split("(")[0].strip()  # drop method ref
    return hx.locate_ra_source(sources_dir, cls)


def _format_block(label, path, code):
    return ("==== %s ====\n# file: %s\n%s" % (label, path, code))


def classify_text(client, code, log_sink=None, tag="", max_retries=4, base_delay=2.0):
    prompt = PROMPT_TEMPLATE.format(code=code)

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            completion = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                top_p=1,
                max_tokens=4096,
                stream=False,
            )
            content = (completion.choices[0].message.content or "").strip()
            if log_sink is not None:
                log_sink.append({
                    "tag": tag, "model": MODEL, "temperature": 0.0, "seed": SEED,
                    "system_fingerprint": getattr(completion, "system_fingerprint", None),
                    "attempt": attempt, "prompt": prompt, "response": content,
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
                log_sink.append({
                    "tag": tag, "model": MODEL, "attempt": attempt,
                    "error": str(e), "prompt": prompt, "response": None,
                })
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


def run_one(client, input_dir, package, log_sink=None):
    loc = locate_ra_java(input_dir, package)
    rec = {
        "fileName": package, "package": package,
        "ra_class": loc["ra_class"], "ra_style": loc["ra_style"],
        "source_path": loc["source_path"], "locate_status": loc["status"],
        "Answer1": "No", "Answer2": "",
        "trail": [], "hops": 0, "stop_reason": "",
    }
    if loc["status"] != "located":
        rec["note"] = "no RA source presented to model (%s)" % loc["status"]
        rec["stop_reason"] = "no_ra"
        return rec

    sources_dir = os.path.join(input_dir, package, "sources")

    # All source seen so far; model sees it all -> verdict co-determined.
    ra_code = read_ra_source(loc["source_path"])
    blocks = [_format_block("RATIONALE ACTIVITY: %s" % loc["ra_class"],
                            loc["source_path"], ra_code)]
    rec["trail"].append({"class": loc["ra_class"], "path": loc["source_path"]})
    visited_paths = {loc["source_path"]}

    answer = {}
    for hop in range(MAX_HOPS + 1):
        rec["hops"] = hop
        combined = "\n\n".join(blocks)
        answer = classify_text(client, combined, log_sink=log_sink,
                               tag="%s#hop%d" % (package, hop))
        decision = (answer.get("Decision") or "").strip().upper()

        if decision == "YES" or answer.get("Answer1") == "Yes":
            rec["Answer1"] = "Yes"
            rec["Answer2"] = answer.get("Answer2", "")
            rec["stop_reason"] = "yes"
            return rec

        if decision == "TERMINAL_DEADEND":
            rec["Answer1"] = "No"
            rec["Answer2"] = answer.get("Answer2", "")
            rec["stop_reason"] = "terminal_deadend"
            return rec

        if decision == "CONTINUE":
            if hop >= MAX_HOPS:
                rec["Answer1"] = "No"
                rec["Answer2"] = answer.get("Answer2", "")
                rec["stop_reason"] = "max_hops"
                return rec
            target = (answer.get("next_target") or "").strip()
            path, method = resolve_target(sources_dir, target)
            if not path or path in visited_paths:
                rec["Answer1"] = "No"
                rec["Answer2"] = answer.get("Answer2", "")
                rec["stop_reason"] = ("unresolved_target:%s" % target if not path
                                      else "revisit:%s" % target)
                rec["trail"].append({"class": target, "path": None,
                                     "resolve": method})
                return rec
            code = read_ra_source(path)
            blocks.append(_format_block("FOLLOWED: %s" % target, path, code))
            visited_paths.add(path)
            rec["trail"].append({"class": target, "path": path,
                                 "resolve": method})
            continue

        rec["Answer1"] = answer.get("Answer1", "No")
        rec["Answer2"] = answer.get("Answer2", "")
        rec["stop_reason"] = "no_decision"
        return rec

    rec["Answer1"] = answer.get("Answer1", "No")
    rec["Answer2"] = answer.get("Answer2", "")
    rec["stop_reason"] = rec["stop_reason"] or "loop_end"
    return rec


def main():
    ap = argparse.ArgumentParser(
        description="Follow-the-trail (RQ2): RA + CONTINUE/TERMINAL_DEADEND, gpt-oss-120b")
    ap.add_argument("--input", required=True, help="base dir containing <package>/ folders")
    ap.add_argument("--output", default="baseline_out", help="output dir")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--package", help="single package to analyze")
    grp.add_argument("--applist", help="text file: one package per line (batch)")
    ap.add_argument("--resume", action="store_true",
                    help="skip packages already present in the output JSON")
    ap.add_argument("--no-logs", action="store_true",
                    help="do not write raw prompt/response logs")
    args = ap.parse_args()

    client = get_client()
    os.makedirs(args.output, exist_ok=True)
    out_file = os.path.join(args.output, "llm_res.json")
    logs_dir = os.path.join(args.output, "logs")
    if not args.no_logs:
        os.makedirs(logs_dir, exist_ok=True)

    if args.package:
        packages = [args.package]
    else:
        raw = open(args.applist, encoding="utf-8-sig").read()
        packages = [ln.strip().lstrip("\ufeff") for ln in raw.splitlines() if ln.strip()]

    results = []
    done = set()
    if args.resume and os.path.isfile(out_file):
        try:
            results = json.load(open(out_file, encoding="utf-8"))
            done = {r.get("package") or r.get("fileName") for r in results}
        except Exception:
            results, done = [], set()

    total = len(packages)
    print("Follow-the-trail: %d apps  ->  %s" % (total, args.output))
    print("Model: %s @ temp=0  (NVIDIA)\n" % MODEL)

    for i, pkg in enumerate(packages, 1):
        if args.resume and pkg in done:
            print("[%d/%d] %-45s SKIP (done)" % (i, total, pkg))
            continue

        log_sink = [] if not args.no_logs else None
        try:
            rec = run_one(client, args.input, pkg, log_sink=log_sink)
            results.append(rec)
            if rec["locate_status"] == "located":
                print("[%d/%d] %-45s OK  (Answer1=%s)" % (i, total, pkg, rec["Answer1"]))
            else:
                print("[%d/%d] %-45s -- %s" % (i, total, pkg, rec["locate_status"]))
        except Exception as err:
            results.append({"fileName": pkg, "package": pkg,
                            "locate_status": "error", "error": str(err)[:300],
                            "Answer1": "No", "Answer2": ""})
            print("[%d/%d] %-45s ERROR: %s" % (i, total, pkg, str(err)[:80]))

        with open(out_file, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, ensure_ascii=False)

        if log_sink is not None:
            with open(os.path.join(logs_dir, pkg + ".log.json"), "w", encoding="utf-8") as lf:
                json.dump(log_sink, lf, indent=2, ensure_ascii=False)

    n_yes = sum(1 for r in results if r.get("Answer1") == "Yes")
    n_loc = sum(1 for r in results if r.get("locate_status") == "located")
    print("\n===== FOLLOW-THE-TRAIL COMPLETE =====")
    print("Records         : %d" % len(results))
    print("RA located      : %d" % n_loc)
    print("Answer1 == Yes  : %d" % n_yes)
    print("\nResults JSON    : %s" % out_file)
    if not args.no_logs:
        print("Raw logs        : %s" % logs_dir)


if __name__ == "__main__":
    main()
