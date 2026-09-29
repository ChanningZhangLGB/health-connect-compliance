#!/usr/bin/env python3
"""
hc_baseline_singleagent_haiku.py — single-agent baseline (RQ2).

One LLM call per app, on ONE located RA Java source. RA located via
hc_extractor (same path as the other baselines). Model: claude-haiku-4-5 on
the Anthropic API, temperature=0.

Prompt caching: the constant instruction block is the system prompt, marked
cache_control ephemeral (5m). Haiku 4.5 only caches prefixes >= 4096 tokens, so
at this prompt size the cache won't hit (cache_read ~0) — expected. Cache token
counts are logged regardless; the follow-up (multi-hop) variant is where the
accumulated prefix exceeds 4096 and the savings appear.

PREREQS: pip install anthropic ; set ANTHROPIC_API_KEY ; hc_extractor.py in ../../utils.

USAGE:
  python hc_baseline_singleagent_haiku.py --input D:\\android_studio_apk_all_decompile_java --applist D:\\test20.txt --output D:\\baseline_out
  python hc_baseline_singleagent_haiku.py --input D:\\android_studio_apk_all_decompile_java --package com.foo --output D:\\baseline_out
"""

import argparse
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "utils"))
import hc_extractor as hx

try:
    from anthropic import Anthropic
except ImportError:
    print("ERROR: pip install anthropic", file=sys.stderr)
    sys.exit(1)

MODEL    = "claude-haiku-4-5-20251001"
SEED     = 42
MAX_TOKENS = 4096
CACHE_TTL  = "5m"
JAVA_LIMIT = 80000

# Prompt split for caching: constant instructions = system, code = user.
SYSTEM_PROMPT = """You are given the contents of a Java source file (provided in the next message).

Please analyze and answer these two questions:
1. Does this code implement an activity that displays app's privacy policy when the user clicks on the privacy policy link in the Health Connect permissions screen? Answer1:[Yes/No]
2. If yes, explain the code that implements the activity. Answer2:[Explanation] 

and respond *only* with a JSON object with keys:
  - "Answer1": "Yes" or "No"
  - "Answer2": a brief explanation (or empty string if Answer1 is "No")

Example of the ONLY acceptable response format:

{
  "Answer1": "Yes",
  "Answer2": "This activity registers a click listener on the privacy-policy link..."
}"""

USER_TEMPLATE = """------------ start of code ------------
{code}
------------ end of code ------------"""


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
    user_text = USER_TEMPLATE.format(code=code)
    system_blocks = [{
        "type": "text",
        "text": SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral", "ttl": CACHE_TTL},
    }]

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                temperature=0.0,
                system=system_blocks,
                messages=[{"role": "user", "content": user_text}],
            )
            content = "".join(b.text for b in msg.content if b.type == "text").strip()
            u = msg.usage
            cache_write = getattr(u, "cache_creation_input_tokens", None)
            cache_read = getattr(u, "cache_read_input_tokens", None)
            if log_sink is not None:
                log_sink.append({
                    "tag": tag, "model": MODEL, "temperature": 0.0, "seed": SEED,
                    "input_tokens": getattr(u, "input_tokens", None),
                    "output_tokens": getattr(u, "output_tokens", None),
                    "cache_creation_input_tokens": cache_write,
                    "cache_read_input_tokens": cache_read,
                    "attempt": attempt, "system": SYSTEM_PROMPT,
                    "user": user_text, "response": content,
                })
            return _parse_answer(content), {
                "cache_creation_input_tokens": cache_write,
                "cache_read_input_tokens": cache_read,
                "input_tokens": getattr(u, "input_tokens", None),
                "output_tokens": getattr(u, "output_tokens", None),
            }
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
                    "error": str(e), "system": SYSTEM_PROMPT,
                    "user": user_text, "response": None,
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
    answer, usage = classify_text(client, code, log_sink=log_sink, tag=package)
    rec["Answer1"] = answer.get("Answer1", "No")
    rec["Answer2"] = answer.get("Answer2", "")
    rec["usage"] = usage
    return rec


def main():
    ap = argparse.ArgumentParser(
        description="Single-agent baseline (RQ2): one RA source, claude-haiku-4-5 + prompt caching")
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
    cw = sum((r.get("usage") or {}).get("cache_creation_input_tokens") or 0 for r in results)
    cr = sum((r.get("usage") or {}).get("cache_read_input_tokens") or 0 for r in results)
    print("\n===== BASELINE COMPLETE =====")
    print("Records           : %d" % len(results))
    print("RA located        : %d" % n_loc)
    print("Answer1 == Yes    : %d" % n_yes)
    print("Cache write tokens: %d" % cw)
    print("Cache read tokens : %d  (expected ~0 at this prompt size)" % cr)
    print("\nResults JSON      : %s" % out_file)
    if not args.no_logs:
        print("Raw logs          : %s" % logs_dir)


# Assert original instruction wording is preserved across the split.
_REQUIRED = [
    "Does this code implement an activity that displays app's privacy policy when the user clicks on the privacy policy link in the Health Connect permissions screen? Answer1:[Yes/No]",
    "If yes, explain the code that implements the activity. Answer2:[Explanation]",
    'respond *only* with a JSON object with keys',
    "------------ start of code ------------",
    "------------ end of code ------------",
]
_combined = SYSTEM_PROMPT + "\n" + USER_TEMPLATE
for _s in _REQUIRED:
    assert _s in _combined, "prompt wording drifted from original: missing %r" % _s


if __name__ == "__main__":
    main()
