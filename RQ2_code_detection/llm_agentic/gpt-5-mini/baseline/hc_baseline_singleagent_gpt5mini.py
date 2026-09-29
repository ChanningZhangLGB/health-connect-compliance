#!/usr/bin/env python3
"""
hc_baseline_singleagent_gpt5mini.py — single-agent baseline (RQ2).

One LLM call per app, on ONE located RA Java source, with the original prompt
preserved verbatim. RA located via hc_extractor (same path as the multi-agent
pipeline). Model: gpt-5-mini on OpenAI (Responses API).

NOTE: GPT-5 family forbids temperature!=1, so this model is NOT reproducible
(documented ~25% verdict-flip rate). reasoning_effort="low"; prompt caching on.

PREREQS: pip install openai ; set OPENAI_API_KEY ; hc_extractor.py in ../../utils.

USAGE:
  python hc_baseline_singleagent_gpt5mini.py --input D:\\android_studio_apk_all_decompile_java --applist D:\\test20.txt --output D:\\baseline_out
  python hc_baseline_singleagent_gpt5mini.py --input D:\\android_studio_apk_all_decompile_java --package com.foo --output D:\\baseline_out
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

MODEL    = "gpt-5-mini"
BASE_URL = None              # OpenAI default endpoint (api.openai.com)
SEED     = 42                # best-effort; GPT-5 family forbids temperature!=1
REASONING_EFFORT = "medium"     # reasoning model; "low" suits classification
CACHE_RETENTION  = "24h"     # extended prompt-cache retention across resumes
CACHE_KEY        = "hc_baseline_singleagent"   # single static key (no agents)
JAVA_LIMIT = 80000

# Original prompt — preserved verbatim (identity asserted at bottom of file).
PROMPT_TEMPLATE = """
You are given the contents of a Java source file below.

------------ start of code ------------
{code}
------------ end of code ------------

Please analyze and answer these two questions:
1. Does this code implement an activity that displays app's privacy policy when the user clicks on the privacy policy link in the Health Connect permissions screen? Answer1:[Yes/No]
2. If yes, explain the code that implements the activity. Answer2:[Explanation] 

and respond *only* with a JSON object with keys:
  - "Answer1": "Yes" or "No"
  - "Answer2": a brief explanation (or empty string if Answer1 is "No")

Example of the ONLY acceptable response format:

{{
  "Answer1": "Yes",
  "Answer2": "This activity registers a click listener on the privacy-policy link..."
}}
"""


def get_client():
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("ERROR: set OPENAI_API_KEY environment variable.", file=sys.stderr)
        sys.exit(1)
    return OpenAI(api_key=key)


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


def classify_text(client, code, log_sink=None, tag="", max_retries=4, base_delay=2.0):
    prompt = PROMPT_TEMPLATE.format(code=code)

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            completion = client.responses.create(
                model=MODEL,
                input=prompt,
                reasoning={"effort": REASONING_EFFORT},
                max_output_tokens=4096,
                prompt_cache_key=CACHE_KEY,
                prompt_cache_retention=CACHE_RETENTION,
            )
            content = (completion.output_text or "").strip()
            cached = None
            try:
                cached = completion.usage.input_tokens_details.cached_tokens
            except Exception:
                pass
            if log_sink is not None:
                log_sink.append({
                    "tag": tag, "model": MODEL, "reasoning_effort": REASONING_EFFORT,
                    "seed": SEED, "cached_tokens": cached,
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
    }
    if loc["status"] != "located":
        rec["note"] = "no RA source presented to model (%s)" % loc["status"]
        return rec

    code = read_ra_source(loc["source_path"])
    answer = classify_text(client, code, log_sink=log_sink, tag=package)
    rec["Answer1"] = answer.get("Answer1", "No")
    rec["Answer2"] = answer.get("Answer2", "")
    return rec


def main():
    ap = argparse.ArgumentParser(
        description="Single-agent baseline (RQ2): one RA source, original prompt, gpt-oss-120b")
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
    print("Baseline (single-agent): %d apps  ->  %s" % (total, args.output))
    print("Model: %s  (OpenAI Responses API, reasoning=%s)\n" % (MODEL, REASONING_EFFORT))

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
    print("\n===== BASELINE COMPLETE =====")
    print("Records         : %d" % len(results))
    print("RA located      : %d" % n_loc)
    print("Answer1 == Yes  : %d" % n_yes)
    print("\nResults JSON    : %s" % out_file)
    if not args.no_logs:
        print("Raw logs        : %s" % logs_dir)


_ORIGINAL_PROMPT = (
    "\nYou are given the contents of a Java source file below.\n\n"
    "------------ start of code ------------\n"
    "{code}\n"
    "------------ end of code ------------\n\n"
    "Please analyze and answer these two questions:\n"
    "1. Does this code implement an activity that displays app's privacy policy "
    "when the user clicks on the privacy policy link in the Health Connect "
    "permissions screen? Answer1:[Yes/No]\n"
    "2. If yes, explain the code that implements the activity. Answer2:[Explanation] \n\n"
    "and respond *only* with a JSON object with keys:\n"
    '  - "Answer1": "Yes" or "No"\n'
    '  - "Answer2": a brief explanation (or empty string if Answer1 is "No")\n\n'
    "Example of the ONLY acceptable response format:\n\n"
    "{{\n"
    '  "Answer1": "Yes",\n'
    '  "Answer2": "This activity registers a click listener on the privacy-policy link..."\n'
    "}}\n"
)
assert PROMPT_TEMPLATE == _ORIGINAL_PROMPT, \
    "PROMPT_TEMPLATE has been modified — it must stay byte-for-byte identical to the original."


if __name__ == "__main__":
    main()
