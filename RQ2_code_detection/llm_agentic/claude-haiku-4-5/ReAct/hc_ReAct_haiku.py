#!/usr/bin/env python3
"""
hc_ReAct_haiku.py — single-agent + follow-the-trail (RQ2).

Baseline = one call on the located RA. This variant: when the RA alone is
inconclusive the model decides CONTINUE (name a next_target) or
TERMINAL_DEADEND; on CONTINUE the class is resolved with hc_extractor, appended,
and re-examined — looping until Yes, dead-end, unresolvable target, or the hop
guard. Final verdict is co-determined over all source gathered.

Prompt caching (the point here): the constant instructions are the cached system
prefix; the accumulated source is the user message with a cache breakpoint on
its last block. Across hops within an app the [instructions + RA + earlier
classes] prefix repeats and is read from cache — this is where Haiku's >=4096
prefix is actually exceeded and the savings appear. Cache token counts logged.

Model: claude-haiku-4-5 on the Anthropic API, temperature=0.

PREREQS: pip install anthropic ; set ANTHROPIC_API_KEY ; hc_extractor.py in ../../utils.

USAGE:
  python hc_ReAct_haiku.py --input D:\\android_studio_apk_all_decompile_java --applist D:\\test20.txt --output D:\\followup_out
  python hc_ReAct_haiku.py --input D:\\android_studio_apk_all_decompile_java --package com.foo --output D:\\followup_out
"""

import argparse
import json
import os
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
SEED     = 42
MAX_TOKENS = 4096
CACHE_TTL  = "5m"
MAX_HOPS = 8
JAVA_LIMIT = 80000

# Prompt split for caching: constant instructions = system, accumulated source = user.
SYSTEM_PROMPT = """You are given the contents of one or more Java source files (provided in the next message).

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

{
  "Answer1": "Yes",
  "Answer2": "This activity registers a click listener on the privacy-policy link...",
  "Decision": "YES",
  "next_target": ""
}

Example when you must follow to another class:

{
  "Answer1": "No",
  "Answer2": "",
  "Decision": "CONTINUE",
  "next_target": "com.example.privacy.PrivacyPolicyActivity"
}

Example when there is nothing here and nothing to follow:

{
  "Answer1": "No",
  "Answer2": "",
  "Decision": "TERMINAL_DEADEND",
  "next_target": ""
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


def resolve_target(sources_dir, target):
    """Resolve next_target to a .java via hc_extractor.locate_ra_source."""
    if not target:
        return (None, "empty_target")
    cls = target.strip().split("#")[0].split("(")[0].strip()  # drop method ref
    return hx.locate_ra_source(sources_dir, cls)


def _format_block(label, path, code):
    return ("==== %s ====\n# file: %s\n%s" % (label, path, code))


def classify_text(client, source_blocks, log_sink=None, tag="", max_retries=4, base_delay=2.0):
    # System = constant instructions (cached). User = accumulated source, wrapped
    # in the original code markers, with a cache breakpoint on the LAST block so
    # the repeated prefix across hops is read from cache.
    system_blocks = [{
        "type": "text",
        "text": SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral", "ttl": CACHE_TTL},
    }]
    joined = "\n\n".join(source_blocks)
    user_text = USER_TEMPLATE.format(code=joined)
    user_content = [{
        "type": "text",
        "text": user_text,
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
                messages=[{"role": "user", "content": user_content}],
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
        "trail": [], "hops": 0, "stop_reason": "",
    }
    if loc["status"] != "located":
        rec["note"] = "no RA source presented to model (%s)" % loc["status"]
        rec["stop_reason"] = "no_ra"
        return rec

    sources_dir = os.path.join(input_dir, package, "sources")

    ra_code = read_ra_source(loc["source_path"])
    blocks = [_format_block("RATIONALE ACTIVITY: %s" % loc["ra_class"],
                            loc["source_path"], ra_code)]
    rec["trail"].append({"class": loc["ra_class"], "path": loc["source_path"]})
    visited_paths = {loc["source_path"]}

    usage_tot = {"cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                 "input_tokens": 0, "output_tokens": 0}

    def accumulate(u):
        for k in usage_tot:
            usage_tot[k] += (u.get(k) or 0)

    answer = {}
    for hop in range(MAX_HOPS + 1):
        rec["hops"] = hop
        answer, usage = classify_text(client, blocks, log_sink=log_sink,
                                      tag="%s#hop%d" % (package, hop))
        accumulate(usage)
        decision = (answer.get("Decision") or "").strip().upper()

        if decision == "YES" or answer.get("Answer1") == "Yes":
            rec["Answer1"] = "Yes"
            rec["Answer2"] = answer.get("Answer2", "")
            rec["stop_reason"] = "yes"
            rec["usage"] = usage_tot
            return rec

        if decision == "TERMINAL_DEADEND":
            rec["Answer1"] = "No"
            rec["Answer2"] = answer.get("Answer2", "")
            rec["stop_reason"] = "terminal_deadend"
            rec["usage"] = usage_tot
            return rec

        if decision == "CONTINUE":
            if hop >= MAX_HOPS:
                rec["Answer1"] = "No"
                rec["Answer2"] = answer.get("Answer2", "")
                rec["stop_reason"] = "max_hops"
                rec["usage"] = usage_tot
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
                rec["usage"] = usage_tot
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
        rec["usage"] = usage_tot
        return rec

    rec["Answer1"] = answer.get("Answer1", "No")
    rec["Answer2"] = answer.get("Answer2", "")
    rec["stop_reason"] = rec["stop_reason"] or "loop_end"
    rec["usage"] = usage_tot
    return rec


def main():
    ap = argparse.ArgumentParser(
        description="Follow-the-trail (RQ2): RA + CONTINUE/TERMINAL_DEADEND, claude-haiku-4-5 + caching")
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
    hops = sum(r.get("hops") or 0 for r in results)
    cw = sum((r.get("usage") or {}).get("cache_creation_input_tokens") or 0 for r in results)
    cr = sum((r.get("usage") or {}).get("cache_read_input_tokens") or 0 for r in results)
    print("\n===== FOLLOW-THE-TRAIL COMPLETE =====")
    print("Records           : %d" % len(results))
    print("RA located        : %d" % n_loc)
    print("Answer1 == Yes    : %d" % n_yes)
    print("Total follow hops : %d" % hops)
    print("Cache write tokens: %d" % cw)
    print("Cache read tokens : %d  (>0 where multi-hop prefix exceeded 4096)" % cr)
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
