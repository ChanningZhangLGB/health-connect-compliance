#!/usr/bin/env python3
"""
rq3_pp_label.py - RQ3 privacy-policy permission-disclosure labeling.

For each app record in merged_admin_labels.json:
  - feed the privacy-policy TEXT + its requested_permission list to an LLM,
  - using the prompt from prompt.docx (system + task), temperature 0,
  - parse the returned JSON array (one object per permission),
  - save results incrementally to <output>/llm_res.json (+ raw logs).

Models (all served via NVIDIA integrate endpoint, NVIDIA_API_KEY):
  gptoss -> openai/gpt-oss-120b
  llama  -> meta/llama-3.3-70b-instruct
  qwen   -> qwen/qwen3-next-80b-a3b-instruct

USAGE (run in a terminal with the REAL python + openai installed):
  python rq3_pp_label.py --model gptoss --input merged_admin_labels.json --output output/gpt-oss-120b --resume
"""

import argparse
import json
import os
import sys
import time

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: pip install openai", file=sys.stderr)
    sys.exit(1)

BASE_URL = "https://integrate.api.nvidia.com/v1"
SEED = 42

MODELS = {
    "gptoss": "openai/gpt-oss-120b",
    "llama":  "meta/llama-3.3-70b-instruct",
    "qwen":   "qwen/qwen3-next-80b-a3b-instruct",
}

# Nominal context window (tokens) for each served model. Used to decide whether
# the entire privacy policy fits (full_policy_loaded flag).
CONTEXT_LIMITS = {
    "gptoss": 131072,
    "llama":  131072,
    "qwen":   262144,
}


def est_tokens(text):
    """Rough token estimate (~4 chars/token)."""
    return (len(text) + 3) // 4

# ---- Prompt from prompt.docx (verbatim) ----------------------------------

SYSTEM_PROMPT = (
    "You are a knowledgeable, helpful, and honest assistant with expertise in "
    "Android privacy, Health Connect permissions, and online privacy policy "
    "analysis. You are experienced in interpreting privacy policies and "
    "identifying whether they explicitly disclose the collection and use of "
    "specific categories of user data.\n"
    "Your goal is to analyze a privacy policy based solely on its textual "
    "content. Do not rely on external knowledge about Android permissions, "
    "Health Connect APIs, or common app functionality.\n"
    "If the privacy policy does not explicitly mention a requested permission or "
    "the corresponding data type, do not infer that it is disclosed.\n"
    "Always support positive decisions with exact quotations from the privacy policy."
)

TASK_PROMPT = """Task Description

You will be given:

(1) A privacy policy.
(2) A list of requested Android permissions (or Health Connect permissions).

For EACH requested permission, determine:

Step 1.
Does the privacy policy explicitly disclose collecting, accessing, reading, storing, processing, or using the data associated with this permission?
Only explicit statements count.
General statements such as "We collect personal information" or "We may collect health information" are NOT sufficient unless they clearly refer to the requested permission or its associated data.

Step 2.
If Step 1 is YES, determine whether the privacy policy explicitly explains WHY the application collects or uses that permission (i.e., the collection purpose or rationale).
The rationale must describe the intended use or purpose of collecting the permission-related data.
Statements that merely repeat that data are collected without explaining their purpose do NOT qualify as rationale.

For each permission, assign labels according to the following rules.

Case 1
If Step 1 = NO
Return
<permission>_not_mentioned

Case 2
If Step 1 = YES and Step 2 = NO
Return
<permission>_specific
<permission>_rationale_no

Case 3
If Step 1 = YES and Step 2 = YES
Return
<permission>_specific
<permission>_rationale_yes

Reasoning Instructions
Let's work through the task carefully.
For each permission:
1. Locate all relevant sentences in the privacy policy.
2. Determine whether the permission (or its corresponding data type) is explicitly mentioned.
3. If mentioned, determine whether the policy explains the purpose of collecting or using that permission.
4. Base every decision only on explicit statements in the privacy policy.
5. Never infer missing information.

Output Format
Return ONLY a JSON array.
Each permission should produce one JSON object with the following fields:

[
  {
    "permission": "...",
    "labels": [ ... ],
    "permission_evidence": [ ... ],
    "rationale_evidence": [ ... ]
  }
]

where
permission        Requested permission.
labels            <permission>_not_mentioned
                  OR <permission>_specific + <permission>_rationale_no
                  OR <permission>_specific + <permission>_rationale_yes
permission_evidence  Exact sentence(s) showing the permission is explicitly mentioned. Return [] if not mentioned.
rationale_evidence   Exact sentence(s) explaining WHY the permission is collected/used. Return [] if no rationale.
"""


def build_user_message(policy, permissions):
    perms = "\n".join("- %s" % p for p in permissions)
    return (
        TASK_PROMPT
        + "\n==================== INPUT ====================\n\n"
        + "Requested Permissions:\n" + perms + "\n\n"
        + "Privacy Policy:\n\"\"\"\n" + policy + "\n\"\"\"\n\n"
        + "================================================\n"
        + "Return ONLY the JSON array described above."
    )


def get_client():
    key = os.environ.get("NVIDIA_API_KEY")
    if not key:
        print("ERROR: set NVIDIA_API_KEY environment variable.", file=sys.stderr)
        sys.exit(1)
    return OpenAI(base_url=BASE_URL, api_key=key)


def _extract_json_array(content):
    t = content.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
        t = t.strip()
    start = t.find("[")
    if start == -1:
        raise ValueError("no JSON array found in response")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(t)):
        c = t[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return json.loads(t[start:i + 1])
    raise ValueError("unbalanced JSON array in response")


def classify(client, model, policy, permissions, max_tokens,
             log_sink=None, tag="", max_retries=4, base_delay=2.0):
    user_msg = build_user_message(policy, permissions)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.0,
                top_p=1,
                max_tokens=max_tokens,
                stream=False,
            )
            content = (completion.choices[0].message.content or "").strip()
            usage = getattr(completion, "usage", None)
            meta = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "finish_reason": getattr(completion.choices[0], "finish_reason", None),
            }
            if log_sink is not None:
                log_sink.append({
                    "tag": tag, "model": model, "temperature": 0.0, "seed": SEED,
                    "attempt": attempt, "usage": meta, "response": content,
                })
            return _extract_json_array(content), content, meta
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                print("  [retry %d/%d after %.0fs] %s: %s"
                      % (attempt, max_retries, delay, tag, str(e)[:160]),
                      file=sys.stderr)
                time.sleep(delay)
            elif log_sink is not None:
                log_sink.append({"tag": tag, "model": model, "attempt": attempt,
                                 "error": str(e)})
    raise last_err


def main():
    ap = argparse.ArgumentParser(description="RQ3 privacy-policy permission labeling")
    ap.add_argument("--model", required=True, choices=list(MODELS),
                    help="gptoss | llama | qwen")
    ap.add_argument("--input", required=True, help="merged_admin_labels.json")
    ap.add_argument("--output", required=True, help="output dir")
    ap.add_argument("--resume", action="store_true",
                    help="skip packages already present in llm_res.json")
    ap.add_argument("--limit", type=int, default=0,
                    help="process only the first N records (0 = all)")
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--no-logs", action="store_true")
    args = ap.parse_args()

    model = MODELS[args.model]
    context_limit = CONTEXT_LIMITS[args.model]
    client = get_client()
    os.makedirs(args.output, exist_ok=True)
    out_file = os.path.join(args.output, "llm_res.json")
    logs_dir = os.path.join(args.output, "logs")
    if not args.no_logs:
        os.makedirs(logs_dir, exist_ok=True)

    data = json.load(open(args.input, encoding="utf-8"))
    if args.limit > 0:
        data = data[:args.limit]

    results = []
    done = set()
    if args.resume and os.path.isfile(out_file):
        try:
            results = json.load(open(out_file, encoding="utf-8"))
            done = {r.get("packagename") for r in results}
        except Exception:
            results, done = [], set()

    total = len(data)
    print("=" * 64)
    print("RQ3 privacy-policy permission labeling")
    print("=" * 64)
    print("  Model          : %s  (alias: %s)" % (model, args.model))
    print("  Endpoint       : %s" % BASE_URL)
    print("  Temperature    : 0.0")
    print("  top_p          : 1")
    print("  seed           : %d" % SEED)
    print("  max_tokens     : %d  (output)" % args.max_tokens)
    print("  context_limit  : %d tokens" % context_limit)
    print("  Prompt         : prompt.docx (system + task), JSON-array output")
    print("  Input          : %s" % args.input)
    print("  Output         : %s" % args.output)
    print("  Records        : %d   resume=%s   logs=%s"
          % (total, bool(args.resume), not args.no_logs))
    print("=" * 64 + "\n")

    for i, rec in enumerate(data, 1):
        pkg = rec.get("packagename")
        perms = rec.get("requested_permission") or []
        policy = str(rec.get("privacy policy") or "")

        if args.resume and pkg in done:
            print("[%d/%d] %-45s SKIP (done)" % (i, total, pkg))
            continue

        # Pre-flight: does the entire policy + prompt fit the model context?
        full_prompt = SYSTEM_PROMPT + build_user_message(policy, perms)
        est_in = est_tokens(full_prompt)
        budget = context_limit - args.max_tokens
        fits_estimate = est_in <= budget

        log_sink = [] if not args.no_logs else None
        out_rec = {
            "packagename": pkg,
            "requested_permission": perms,
            "policy_chars": len(policy),
            "est_input_tokens": est_in,
            "context_limit": context_limit,
            "fits_context_estimate": fits_estimate,
        }
        try:
            parsed, raw, meta = classify(client, model, policy, perms,
                                         args.max_tokens, log_sink=log_sink, tag=pkg)
            out_rec["result"] = parsed
            out_rec["raw_response"] = raw
            out_rec["prompt_tokens"] = meta["prompt_tokens"]
            out_rec["completion_tokens"] = meta["completion_tokens"]
            out_rec["finish_reason"] = meta["finish_reason"]
            # Request succeeded => the API accepted (did not reject for context
            # length), so the full policy was loaded into the model.
            out_rec["full_policy_loaded"] = True
            out_rec["output_truncated"] = (meta["finish_reason"] == "length")
            out_rec["status"] = "ok"
            n = len(parsed) if isinstance(parsed, list) else 0
            ptok = meta["prompt_tokens"]
            flag = "FULL"
            extra = "  [OUTPUT TRUNCATED]" if out_rec["output_truncated"] else ""
            print("[%d/%d] %-42s OK (%d perms)  in=%s tok  %s%s"
                  % (i, total, pkg, n, ptok, flag, extra))
        except Exception as err:
            msg = str(err)
            ctx_overflow = (not fits_estimate) or any(
                s in msg.lower() for s in
                ("context length", "maximum context", "context_length",
                 "too long", "max_tokens", "input is too large"))
            out_rec["result"] = None
            out_rec["status"] = "error"
            out_rec["error"] = msg[:300]
            # Only assert NOT-loaded when the failure is a context/length issue;
            # other errors (network, rate limit) leave load status undetermined.
            out_rec["full_policy_loaded"] = False if ctx_overflow else None
            flag = "PARTIAL/NOT-LOADED" if ctx_overflow else "load=?"
            print("[%d/%d] %-42s ERROR [%s]: %s"
                  % (i, total, pkg, flag, msg[:70]))

        results.append(out_rec)
        with open(out_file, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, ensure_ascii=False)
        if log_sink is not None:
            safe = (pkg or "unknown").replace("/", "_")
            with open(os.path.join(logs_dir, safe + ".log.json"), "w",
                      encoding="utf-8") as lf:
                json.dump(log_sink, lf, indent=2, ensure_ascii=False)

    n_ok = sum(1 for r in results if r.get("status") == "ok")
    n_err = sum(1 for r in results if r.get("status") == "error")
    n_full = sum(1 for r in results if r.get("full_policy_loaded") is True)
    n_partial = sum(1 for r in results if r.get("full_policy_loaded") is False)
    n_outtrunc = sum(1 for r in results if r.get("output_truncated"))
    print("\n===== RQ3 COMPLETE (%s) =====" % args.model)
    print("Records : %d   ok=%d   error=%d" % (len(results), n_ok, n_err))
    print("Full policy loaded : %d   NOT fully loaded : %d" % (n_full, n_partial))
    print("Output truncated   : %d  (answer hit max_tokens; consider raising --max-tokens)"
          % n_outtrunc)
    print("Results : %s" % out_file)


if __name__ == "__main__":
    main()
