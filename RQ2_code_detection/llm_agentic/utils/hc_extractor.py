#!/usr/bin/env python3
"""
hc_extractor.py  —  Health Connect privacy-policy compliance evidence extractor.

Walks a JADX-decompiled app tree and produces ONE structured JSON per app
capturing the static evidence chain that determines whether the HC "Read
privacy policy" link is reachable:

    Manifest rationale Activity  ->  RA source (onCreate behavior)
        ->  navigation chain (followed while policy-related code is found)
        ->  layouts + strings + bundled policy files along the chain

Expected input layout (one folder per package):

    <base>/<package>/resources/AndroidManifest.xml
    <base>/<package>/resources/res/{layout,values,xml}/...
    <base>/<package>/resources/assets/...
    <base>/<package>/sources/<pkg/path>/Class.java

Usage:
    python hc_extractor.py --input  D:\\android_studio_apk_all_decompile_java \\
                           --output D:\\rq2_evidence_json
    python hc_extractor.py --input ... --output ... --package com.myfitnesspal.android
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import deque

ANDROID_NS = "http://schemas.android.com/apk/res/android"
A = "{%s}" % ANDROID_NS  # android: namespace prefix for ElementTree

# Intents that identify the rationale Activity (RA)
RA_ACTION = "androidx.health.ACTION_SHOW_PERMISSIONS_RATIONALE"
RA_CATEGORY = "android.intent.category.HEALTH_PERMISSIONS"

# Decompiled-source roots. The full app corpus is split across two directories,
# so resolve_app_root() searches the caller's --input dir first and then these
# extras, letting the pipeline find an app in whichever one actually contains it.
# Optional extra decompiled-source roots searched in addition to --input.
# Left empty for the public artifact; the original study split its corpus across
# two drives. Add absolute paths here if your apps live in more than one folder.
EXTRA_SOURCE_ROOTS = []


def resolve_app_root(input_dir, package):
    """Return <root>/<package> for the first root that actually contains the
    app, searching `input_dir` first and then EXTRA_SOURCE_ROOTS. If the app is
    in none of them, return <input_dir>/<package> unchanged so the caller's
    existing os.path.isdir() check still reports 'app_folder_missing'.
    """
    seen = set()
    for root in [input_dir] + EXTRA_SOURCE_ROOTS:
        if not root:
            continue
        key = os.path.normcase(os.path.abspath(root))
        if key in seen:
            continue
        seen.add(key)
        cand = os.path.join(root, package)
        if os.path.isdir(cand):
            return cand
    return os.path.join(input_dir, package)

# What counts as "policy-related" — used to gate chain following and to
# score nodes. Tuned to be inclusive (recall over precision at the node level;
# the chain structure provides precision).
POLICY_REGEX = re.compile(
    r"privacy|policy|legal|terms|consent|gdpr|rationale|disclaimer|"
    r"data[\s_-]?protection|data[\s_-]?handling|permissions?[\s_-]?rationale",
    re.IGNORECASE,
)
URL_REGEX = re.compile(r"https?://[^\s\"'<>\\)]+", re.IGNORECASE)

# Hints that a destination is the app's *general* entry point rather than a
# dedicated policy screen (the "buried link" signal).
GENERAL_ENTRY_REGEX = re.compile(
    r"(^|\.)(Main|Home|Launcher|Splash|Onboarding|Welcome|Login|SignIn|"
    r"SignUp|Register|Auth|Dashboard|Landing|Intro|Start)Activity$",
    re.IGNORECASE,
)

# Safety caps so we never walk the whole app graph (overridable via CLI)
MAX_NODES = 60
MAX_DEPTH = 10


# --------------------------------------------------------------------------
# Manifest parsing
# --------------------------------------------------------------------------
def _localname(tag):
    return tag.split("}", 1)[-1]


def resolve_class(name, pkg):
    """Resolve a manifest android:name to a fully-qualified class."""
    if not name:
        return None
    if name.startswith("."):
        return pkg + name
    if "." not in name:
        return pkg + "." + name
    return name


def parse_manifest(manifest_path, pkg_fallback):
    out = {
        "package": pkg_fallback,
        "rationale_activity": {"declared": False},
        "internet_permission": False,
        "uses_cleartext_traffic": "unset",
        "network_security_config": {"declared": False},
        "hc_permissions": [],
    }
    if not os.path.isfile(manifest_path):
        out["error"] = "AndroidManifest.xml not found"
        return out
    try:
        tree = ET.parse(manifest_path)
        root = tree.getroot()
    except Exception as e:
        out["error"] = "manifest parse failed: %s" % e
        return out

    pkg = root.get("package") or pkg_fallback
    out["package"] = pkg

    # permissions
    for perm in root.findall("uses-permission"):
        n = perm.get(A + "name") or ""
        if n == "android.permission.INTERNET":
            out["internet_permission"] = True
        if "permission.health" in n or ".health." in n.lower():
            out["hc_permissions"].append(n)

    app = root.find("application")
    if app is not None:
        ct = app.get(A + "usesCleartextTraffic")
        if ct is not None:
            out["uses_cleartext_traffic"] = ct
        nsc = app.get(A + "networkSecurityConfig")
        if nsc:
            out["network_security_config"] = {"declared": True, "ref": nsc}

    # Collect activities and aliases to resolve the RA implementation class
    activities = {}      # name -> element
    aliases = []
    if app is not None:
        for el in app.iter():
            ln = _localname(el.tag)
            if ln == "activity":
                nm = resolve_class(el.get(A + "name"), pkg)
                if nm:
                    activities[nm] = el
            elif ln == "activity-alias":
                aliases.append(el)

    def has_ra_filter(el):
        for intent in el.findall("intent-filter"):
            actions = {a.get(A + "name") for a in intent.findall("action")}
            cats = {c.get(A + "name") for c in intent.findall("category")}
            if RA_ACTION in actions or RA_CATEGORY in cats:
                return True, (RA_ACTION if RA_ACTION in actions else None), \
                       (RA_CATEGORY if RA_CATEGORY in cats else None)
        return False, None, None

    ra = out["rationale_activity"]
    candidates = []  # ordered list of (class, how) to try locating, best-first

    # A14-style: alias carrying the RA filter, pointing at targetActivity
    a14_target = None
    a14_alias_name = None
    a14 = None
    for al in aliases:
        ok, act, cat = has_ra_filter(al)
        if ok:
            a14_target = resolve_class(al.get(A + "targetActivity"), pkg)
            a14_alias_name = resolve_class(al.get(A + "name"), pkg)
            a14 = (act, cat)
            break

    # A13-style: activity directly carrying the RA filter
    a13_class = None
    a13 = None
    for nm, el in activities.items():
        ok, act, cat = has_ra_filter(el)
        if ok:
            a13_class = nm
            a13 = (act, cat)
            break

    # Build the candidate list. Order matters (most-authoritative first), but the
    # locator will try EACH until one resolves to a real source file — this fixes
    # the A14-alias-with-divergent-target case where targetActivity has no
    # standalone .java but the real implementation (e.g. MainActivity) does.
    if a14_target:
        candidates.append(a14_target)
    if a13_class and a13_class not in candidates:
        candidates.append(a13_class)
    if a14_alias_name and a14_alias_name not in candidates:
        candidates.append(a14_alias_name)

    if candidates:
        primary_style = "A14_alias" if a14_target else "A13_activity"
        act, cat = (a14 if a14_target else a13) or (None, None)
        ra.update({
            "declared": True,
            "style": primary_style,
            "intent_action": act,
            "intent_category": cat,
            "via_alias": bool(a14_target),
            "implementation_class": candidates[0],   # primary (back-compat)
            "candidate_classes": candidates,          # NEW: all classes to try
            "alias_name": a14_alias_name,
        })

    return out


def load_network_security_config(res_dir, manifest_meta):
    nsc = manifest_meta.get("network_security_config", {})
    if not nsc.get("declared"):
        return nsc
    ref = nsc.get("ref", "")
    name = ref.split("/")[-1] if "/" in ref else ref.lstrip("@").split("/")[-1]
    path = os.path.join(res_dir, "xml", name + ".xml")
    nsc["path"] = path
    if os.path.isfile(path):
        try:
            txt = open(path, encoding="utf-8", errors="replace").read()
            nsc["content"] = txt[:4000]
            m = re.search(r'cleartextTrafficPermitted\s*=\s*"(true|false)"', txt)
            nsc["cleartext_permitted"] = (m.group(1) == "true") if m else None
        except Exception as e:
            nsc["error"] = str(e)
    else:
        nsc["found"] = False
    return nsc


# --------------------------------------------------------------------------
# Java source analysis
# --------------------------------------------------------------------------
def class_to_source_path(sources_dir, fqcn):
    if not fqcn:
        return None
    rel = fqcn.replace(".", os.sep) + ".java"
    p = os.path.join(sources_dir, rel)
    return p if os.path.isfile(p) else None


def locate_ra_source(sources_dir, fqcn):
    """Authoritative RA-source locator (Steps 3-6), I/O-minimal.
    Returns (path, method):
      method = 'direct'         -> file at FQCN->path (Step 4)               [1 stat]
      method = 'inner_path'     -> outer-class path via '$' stripping        [1 stat]
      method = 'package_scan'   -> declared in a same-package file           [bounded]
      method = 'not_found'      -> genuinely absent (Step 6); path None
    """
    if not fqcn:
        return (None, "no_class")

    # ---- Step 4: direct path hit (single filesystem stat, no read) ----
    rel = fqcn.replace(".", os.sep) + ".java"
    direct = os.path.join(sources_dir, rel)
    if os.path.isfile(direct):
        return (direct, "direct")

    # ---- Step 5a: inner class -> outer file by '$' / dotted-nested path ----
    # Inner classes live in the OUTER class's file, at a deterministic path.
    # com.foo.Outer$Inner -> sources/com/foo/Outer.java
    # Also handle JADX dotted nesting (com.foo.Outer.Inner) by peeling the
    # trailing simple-name segments one at a time -- all stat-only, no reads.
    parts = fqcn.split(".")
    last = parts[-1]
    if "$" in last:
        outer = last.split("$")[0]
        cand = os.path.join(sources_dir, *parts[:-1], outer + ".java")
        if os.path.isfile(cand):
            return (cand, "inner_path")
    else:
        # peel possible nested-class segments: a.b.C.D.E -> try .../C/D/E,
        # .../C/D, .../C  (outer classes are capitalized; stop sensibly)
        for cut in range(len(parts) - 1, 0, -1):
            cand = os.path.join(sources_dir, *parts[:cut]) + ".java"
            if os.path.isfile(cand):
                return (cand, "inner_path")

    # ---- Step 5b: bounded scan of the OWNING package folder only ----
    # Never walks the whole tree. Lists one directory, filters by filename,
    # reads only the few files whose name could plausibly hold the class.
    simple = last.split("$")[0]
    pkg_dir = os.path.join(sources_dir, *parts[:-1])
    if os.path.isdir(pkg_dir):
        try:
            entries = [f for f in os.listdir(pkg_dir) if f.endswith(".java")]
        except OSError:
            entries = []
        # exact filename first (cheapest, most likely)
        exact = simple + ".java"
        if exact in entries:
            return (os.path.join(pkg_dir, exact), "package_scan")
        # otherwise read only same-package files and look for the declaration
        decl = re.compile(r"\b(?:class|interface)\s+" + re.escape(simple) + r"\b")
        for f in entries:
            fp = os.path.join(pkg_dir, f)
            try:
                if decl.search(open(fp, encoding="utf-8", errors="replace").read()):
                    return (fp, "package_scan")
            except Exception:
                continue

    # ---- Step 6: authoritative absence (no full-tree walk) ----
    return (None, "not_found")


# --------------------------------------------------------------------------
# Diagnostic sub-classification of un-located apps
# --------------------------------------------------------------------------
# Why an RA didn't resolve, for apps where locate_ra_source() returned None.
# This is DIAGNOSTIC ONLY: it never changes the located / not-found accounting
# (the app stays not-located) — it just explains the miss so error analysis can
# separate "no source exists to find" from "source exists but was renamed".

# Game/cross-platform engines whose RA ships as a stock SDK class in native or
# engine code that JADX does not decompile into sources/ — so there is genuinely
# no .java to locate. Matched against the declared RA FQCN.
ENGINE_PACKAGE_PREFIXES = (
    "com.unity3d.",      # Unity
    "com.epicgames.",    # Unreal
    "org.cocos2dx.",     # Cocos2d-x
    "org.godotengine.",  # Godot
)

# Cheap framework markers: a single isdir() each, no tree walk. The RA impl for
# these stacks lives in framework/native land that JADX omits.
_FRAMEWORK_MARKERS = (
    ("framework_stub:unity",        os.path.join("com", "unity3d")),
    ("framework_stub:react_native", os.path.join("com", "facebook", "react")),
    ("framework_stub:flutter",      os.path.join("io", "flutter")),
)


def _looks_obfuscated(sources_dir):
    """Heuristic: a tree dominated by very short top-level package dirs
    (a/, a0/, b1/, xsna/ …) is the signature of name-mangling obfuscation."""
    try:
        tops = [d for d in os.listdir(sources_dir)
                if os.path.isdir(os.path.join(sources_dir, d))]
    except OSError:
        return False
    if not tops:
        return False
    short = sum(1 for d in tops if len(d) <= 2)
    return short >= 3 and short / len(tops) >= 0.25


def _dir_has_mangled_names(pkg_dir):
    """True if a package dir holds single-char .java class files (d.java, f.java).
    Real code never names a class 'd'; this is identifier-mangling obfuscation.
    Catches the common case where only the app's own package is renamed while the
    bundled library packages (androidx/, kotlin/ …) keep their names, so the
    whole-tree heuristic in _looks_obfuscated misses it. 'R' is excluded (the
    Android resource class is a legitimate single-letter name)."""
    try:
        names = [f[:-5] for f in os.listdir(pkg_dir) if f.endswith(".java")]
    except OSError:
        return False
    return any(len(n) == 1 and n != "R" for n in names)


def classify_unlocated_reason(sources_dir, ra):
    """Refine why an app's RA source did not resolve. Returns one of:
      'no_ra_declared'              -> manifest has no HC rationale intent-filter
      'framework_stub:<engine>'     -> RA is a stock engine/RN/Flutter class (no .java emitted)
      'obfuscated_rename'           -> sources present but name-mangled; declared FQCN renamed
      'truly_missing'               -> declared, decompiled, not a stub, not obfuscated — genuinely absent
    DIAGNOSTIC ONLY — does not affect located/not-found counts.
    """
    if not ra.get("declared"):
        return "no_ra_declared"
    for c in (ra.get("candidate_classes") or []):
        if any(c.startswith(p) for p in ENGINE_PACKAGE_PREFIXES):
            return "framework_stub:engine"
    for label, rel in _FRAMEWORK_MARKERS:
        if os.path.isdir(os.path.join(sources_dir, rel)):
            return label
    # obfuscation: the declared RA's own package was identifier-mangled (its class
    # renamed away, leaving single-char siblings) — check each candidate's package
    for c in (ra.get("candidate_classes") or []):
        pkg_dir = os.path.join(sources_dir, *c.split(".")[:-1])
        if _dir_has_mangled_names(pkg_dir):
            return "obfuscated_rename"
    if _looks_obfuscated(sources_dir):
        return "obfuscated_rename"
    return "truly_missing"


# ViewBinding: `FooBinding.inflate(...)` then `setContentView(root)`.
# Layout name is the binding class de-camel-cased:
#   PermissionsRationaleActivityBinding -> permissions_rationale_activity
RE_VIEWBINDING = re.compile(r"(\w+)Binding\.inflate\s*\(")
RE_WEBVIEW_FIELD = re.compile(r"\.(\w*[Ww]eb\w*)\.(?:loadUrl|loadData|getSettings)")
RE_WEBVIEW_REF = re.compile(r"\.(?:loadUrl|loadData|loadDataWithBaseURL)\s*\(\s*([\w$.]+)\s*[,)]")
RE_JS_ENABLED = re.compile(r"setJavaScriptEnabled\s*\(\s*true\s*\)")


def _const_def_regex(const_name):
    return re.compile(r'static\s+final\s+String\s+' + re.escape(const_name) + r'\s*=\s*"((?:[^"\\]|\\.)*)"')


def camel_to_snake_layout(binding_class):
    name = re.sub(r"Binding$", "", binding_class)
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def extract_imports(src):
    return set(re.findall(r"import\s+([\w$.]+)\s*;", src))


def resolve_constant(ref, sources_dir, pkg, imports, cur_src, _seen=None):
    """Resolve 'SomeClass.CONST' or bare 'CONST' to its String value.
    Searches current file, then the owning class (via imports / same package).
    One cross-file hop. Returns str|None."""
    if _seen is None:
        _seen = set()
    if ref in _seen or len(_seen) > 4:
        return None
    _seen.add(ref)
    if "." in ref:
        cls, const = ref.rsplit(".", 1)
    else:
        cls, const = None, ref
    m = _const_def_regex(const).search(cur_src)
    if m:
        return m.group(1)
    if cls:
        candidates = []
        for imp in imports:
            if imp.endswith("." + cls) or imp == cls:
                candidates.append(imp)
        candidates.append(pkg + "." + cls)
        for fqcn in candidates:
            p = class_to_source_path(sources_dir, fqcn)
            if p:
                try:
                    txt = open(p, encoding="utf-8", errors="replace").read()
                except Exception:
                    continue
                mm = _const_def_regex(const).search(txt)
                if mm:
                    return mm.group(1)
    return None


# regexes over decompiled Java (JADX). R may be renamed (e.g. C0123R), so we
# match the trailing ".layout.NAME" / ".string.NAME" rather than a fixed "R".
RE_SETCONTENT = re.compile(r"setContentView\s*\(\s*[\w$.]*\.layout\.(\w+)")
RE_INFLATE = re.compile(r"\.inflate\s*\(\s*[\w$.]*\.layout\.(\w+)")
RE_LAYOUT_REF = re.compile(r"\.layout\.(\w+)")
RE_STRING_REF = re.compile(r"\.string\.(\w+)")
RE_WEBVIEW = re.compile(
    r"\.(loadUrl|loadData|loadDataWithBaseURL)\s*\(\s*([^;]{0,400}?)\)", re.DOTALL
)
RE_STRLIT = re.compile(r'"((?:[^"\\]|\\.)*)"')
# startActivity(new Intent(x, Foo.class)) / setClass(x, Foo.class) / Intent(x, Foo.class)
RE_INTENT_CLASS = re.compile(r"(?:new\s+Intent|setClass(?:Name)?)\s*\([^;]*?([\w$.]+)\.class")
RE_START_ACT = re.compile(r"startActivity(?:ForResult)?\s*\(")


def extract_string_literals(snippet):
    return [m.group(1) for m in RE_STRLIT.finditer(snippet)]


def slice_method(src, method_name):
    """Return the brace-balanced body of the first *real* method declaration
    matching method_name. Anchors on a method signature (return type +
    name + '(') so it skips @Metadata/annotation arrays that merely mention
    the name as a string (e.g. JADX's  mv = {2, 0, 0}  after a "onCreate"
    token inside @Metadata(d2 = {...})). Returns '' if not found."""
    # Signature: optional modifiers/return type, the method name, then '(' ... ')' '{'
    # Require a non-quote, non-comma char before '{' so annotation arrays like
    # {"...","onCreate",...} can't match.
    sig = re.compile(
        r"(?:public|protected|private|final|static|synchronized|\s)+"
        r"[\w<>\[\]$.?]+\s+" + re.escape(method_name) + r"\s*\([^;{]*\)\s*\{"
    )
    m = sig.search(src)
    if not m:
        # fallback: method name immediately followed by '(' and a '{' (covers
        # cases where the return type regex is too strict), but still require
        # the '(' so a bare string mention won't match.
        alt = re.compile(re.escape(method_name) + r"\s*\([^;{)]*\)\s*\{")
        m = alt.search(src)
        if not m:
            return ""
    brace = src.index("{", m.end() - 1)
    depth = 0
    i = brace
    while i < len(src):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[brace : i + 1]
        i += 1
    return ""


def analyze_java(fqcn, src, known_classes, pkg, sources_dir=None):
    """Parse one decompiled Java class for policy-relevant behavior."""
    imports = extract_imports(src)
    body = ""
    for m in ("onCreate", "onStart", "onResume", "onViewCreated"):
        body += "\n" + slice_method(src, m)
    if not body.strip():
        body = src

    # --- layouts: classic setContentView(R.layout.X) AND ViewBinding ---
    layouts = set(RE_SETCONTENT.findall(src) + RE_INFLATE.findall(src))
    binding_layouts = []
    for bc in RE_VIEWBINDING.findall(src):
        lay = camel_to_snake_layout(bc + "Binding")
        binding_layouts.append(lay)
        layouts.add(lay)

    behavior = {
        "set_content_views": sorted(layouts),
        "viewbinding_layouts": sorted(set(binding_layouts)),
        "layout_refs": sorted(set(RE_LAYOUT_REF.findall(src))),
        "string_refs": sorted(set(RE_STRING_REF.findall(src))),
        "webviews": [],
        "webview_fields": sorted(set(RE_WEBVIEW_FIELD.findall(src))),
        "js_enabled": bool(RE_JS_ENABLED.search(src)),
        "start_activities": [],
        "urls": sorted(set(URL_REGEX.findall(src))),
        "finishes": "finish()" in src,
    }

    # --- WebView loads: literal args first ---
    seen_args = set()
    for m in RE_WEBVIEW.finditer(src):
        method, arg = m.group(1), m.group(2).strip()
        seen_args.add(arg[:60])
        lits = extract_string_literals(arg)
        url = next((l for l in lits if l.startswith("http") or l.startswith("file")
                    or l.endswith(".html") or l.endswith(".pdf")), lits[0] if lits else None)
        behavior["webviews"].append({
            "method": method,
            "arg_excerpt": arg[:200],
            "url_literal": url,
            "resolved_from_constant": False,
            "is_policy_url": bool(url and POLICY_REGEX.search(url)),
            "is_cleartext": bool(url and url.lower().startswith("http://")),
            "is_local": bool(url and (url.startswith("file:") or "android_asset" in (url or "")
                                      or (url.endswith(".html")))),
            "dynamic": not bool(lits),
        })

    # --- WebView loads where the arg is a CONSTANT reference ---
    for m in RE_WEBVIEW_REF.finditer(src):
        ref = m.group(1)
        if ref.split(".")[-1] in ("toString", "getUrl") or '"' in ref:
            continue
        # skip if this looks like a plain literal already captured
        resolved = None
        if sources_dir is not None and re.search(r"[A-Z_]{3,}$", ref.split(".")[-1]):
            resolved = resolve_constant(ref, sources_dir, pkg, imports, src)
        if resolved:
            behavior["webviews"].append({
                "method": "loadUrl",
                "arg_excerpt": ref,
                "url_literal": resolved,
                "resolved_from_constant": True,
                "is_policy_url": bool(POLICY_REGEX.search(resolved)),
                "is_cleartext": resolved.lower().startswith("http://"),
                "is_local": resolved.startswith("file:") or "android_asset" in resolved
                            or resolved.endswith(".html"),
                "dynamic": False,
            })
            if POLICY_REGEX.search(resolved):
                behavior["urls"] = sorted(set(behavior["urls"] + [resolved]))

    if RE_START_ACT.search(src):
        for m in RE_INTENT_CLASS.finditer(src):
            cls = m.group(1)
            cand = resolve_class(cls, pkg) if "." not in cls else cls
            target = cand if cand in known_classes else (
                next((k for k in known_classes if k.endswith("." + cls)), cand))
            behavior["start_activities"].append({
                "raw": cls,
                "target": target,
                "in_app": target in known_classes,
                "looks_general_entry": bool(GENERAL_ENTRY_REGEX.search(target or cls)),
            })

    return behavior


def score_policy_signals(fqcn, behavior, layouts, policy_strings_hit):
    sig = []
    if POLICY_REGEX.search(fqcn):
        sig.append("class_name")
    for lay in behavior["set_content_views"] + behavior["layout_refs"]:
        if POLICY_REGEX.search(lay):
            sig.append("layout_name:" + lay)
    for wv in behavior["webviews"]:
        if wv["is_policy_url"]:
            sig.append("webview_policy_url")
        elif wv["url_literal"]:
            sig.append("webview_url")
    for fld in behavior.get("webview_fields", []):
        if POLICY_REGEX.search(fld):
            sig.append("webview_field:" + fld)
        else:
            sig.append("webview_present")
    for u in behavior["urls"]:
        if POLICY_REGEX.search(u):
            sig.append("url:policy")
    for s in behavior["string_refs"]:
        if POLICY_REGEX.search(s):
            sig.append("string_ref:" + s)
    if policy_strings_hit:
        sig.append("string_value_policy")
    for lay in layouts:
        if lay.get("has_webview"):
            sig.append("layout_webview:" + lay["name"])
        if lay.get("policy_text_hits"):
            sig.append("layout_policy_text:" + lay["name"])
    return sorted(set(sig))


def load_strings(res_dir):
    """name -> value, merged from values*/strings.xml."""
    out = {}
    if not os.path.isdir(res_dir):
        return out
    for d in os.listdir(res_dir):
        if not d.startswith("values"):
            continue
        sp = os.path.join(res_dir, d, "strings.xml")
        if os.path.isfile(sp):
            try:
                root = ET.parse(sp).getroot()
                for s in root.findall("string"):
                    nm = s.get("name")
                    if nm:
                        out[nm] = "".join(s.itertext()).strip()
            except Exception:
                pass
    return out


def load_layout(res_dir, name):
    for d in os.listdir(res_dir) if os.path.isdir(res_dir) else []:
        if not d.startswith("layout"):
            continue
        p = os.path.join(res_dir, d, name + ".xml")
        if os.path.isfile(p):
            try:
                txt = open(p, encoding="utf-8", errors="replace").read()
            except Exception:
                return None
            policy_hits = sorted(set(
                m.group(0) for m in POLICY_REGEX.finditer(txt)))[:8]
            return {
                "name": name,
                "path": p,
                "has_webview": "WebView" in txt or "webkit.WebView" in txt,
                "policy_text_hits": policy_hits,
                "content_excerpt": txt[:1500],
            }
    return {"name": name, "found": False}


def find_bundled_policy(app_root):
    hits = []
    for sub in ("resources/assets", "resources/res/raw"):
        base = os.path.join(app_root, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, _, files in os.walk(base):
            for f in files:
                low = f.lower()
                if (POLICY_REGEX.search(low) or low.endswith(".html")
                        or low.endswith(".pdf")):
                    fp = os.path.join(dirpath, f)
                    try:
                        sz = os.path.getsize(fp)
                    except OSError:
                        sz = None
                    hits.append({
                        "path": os.path.relpath(fp, app_root),
                        "size": sz,
                        "policy_named": bool(POLICY_REGEX.search(low)),
                    })
    return hits


# --------------------------------------------------------------------------
# Per-app driver
# --------------------------------------------------------------------------
def index_sources(sources_dir):
    """Set of FQCNs available as .java files (for in-app resolution)."""
    classes = set()
    if not os.path.isdir(sources_dir):
        return classes
    for dp, _, files in os.walk(sources_dir):
        for f in files:
            if f.endswith(".java"):
                rel = os.path.relpath(os.path.join(dp, f), sources_dir)
                classes.add(rel[:-5].replace(os.sep, "."))
    return classes


def extract_app(app_root, package, cfg=None):
    if cfg is None:
        cfg = {}
    layers = cfg.get("layers", {"manifest", "java", "resources", "bundled"})
    chain_mode = cfg.get("chain_mode", "policy")   # ra_only|hop1|hop2|policy
    max_nodes = cfg.get("max_nodes", MAX_NODES)
    max_depth_cap = cfg.get("max_depth", MAX_DEPTH)

    if chain_mode == "ra_only":
        hop_ceiling, gate_on_policy = 0, False
    elif chain_mode == "hop1":
        hop_ceiling, gate_on_policy = 1, False
    elif chain_mode == "hop2":
        hop_ceiling, gate_on_policy = 2, False
    else:  # "policy"
        hop_ceiling, gate_on_policy = max_depth_cap, True

    res_dir = os.path.join(app_root, "resources")
    sources_dir = os.path.join(app_root, "sources")
    res_res = os.path.join(res_dir, "res")
    manifest_path = os.path.join(res_dir, "AndroidManifest.xml")

    result = {
        "package": package,
        "paths": {"app_root": app_root},
        "config": {"chain_mode": chain_mode, "layers": sorted(layers),
                   "max_nodes": max_nodes, "max_depth": max_depth_cap},
        "errors": [],
    }

    manifest = parse_manifest(manifest_path, package)
    if "manifest" in layers:
        manifest["network_security_config"] = load_network_security_config(res_res, manifest)
    result["manifest"] = manifest

    strings = load_strings(res_res) if "resources" in layers else {}
    known = index_sources(sources_dir)

    ra = manifest["rationale_activity"]
    chain = {"nodes": [], "edges": [], "terminated_reason": None}

    if "java" not in layers:
        chain["terminated_reason"] = "java_layer_disabled"
    elif not ra.get("declared"):
        chain["terminated_reason"] = "no_rationale_activity_declared"
    else:
        root_cls = ra.get("implementation_class")
        if not class_to_source_path(sources_dir, root_cls):
            chain["terminated_reason"] = "ra_source_not_found"
            chain["ra_class"] = root_cls

        visited = set()
        queue = deque()
        if root_cls:
            queue.append((root_cls, 0))

        while queue and len(chain["nodes"]) < max_nodes:
            fqcn, depth = queue.popleft()
            if fqcn in visited or depth > max_depth_cap:
                continue
            visited.add(fqcn)

            sp = class_to_source_path(sources_dir, fqcn)
            node = {
                "class": fqcn,
                "depth": depth,
                "is_rationale_activity": (fqcn == root_cls),
                "source_found": bool(sp),
            }
            if not sp:
                node["policy_related"] = False
                chain["nodes"].append(node)
                continue

            try:
                src_j = open(sp, encoding="utf-8", errors="replace").read()
            except Exception as e:
                node["error"] = str(e)
                chain["nodes"].append(node)
                continue

            behavior = analyze_java(fqcn, src_j, known, manifest["package"], sources_dir)

            layouts = []
            if "resources" in layers:
                for lay in behavior["set_content_views"]:
                    layouts.append(load_layout(res_res, lay))

            ps_hit = any(POLICY_REGEX.search(strings.get(s, "")) for s in behavior["string_refs"])

            signals = score_policy_signals(fqcn, behavior, layouts, ps_hit)
            node["policy_related"] = bool(signals)
            node["policy_signals"] = signals
            node["behavior"] = behavior
            node["layouts"] = layouts
            node["onCreate_excerpt"] = slice_method(src_j, "onCreate")[:1200]
            chain["nodes"].append(node)

            if depth >= hop_ceiling:
                continue
            if gate_on_policy:
                allow_expand = node["policy_related"] or node["is_rationale_activity"]
            else:
                allow_expand = True
            if allow_expand:
                for sa in behavior["start_activities"]:
                    tgt = sa.get("target")
                    if tgt and sa.get("in_app") and tgt not in visited:
                        chain["edges"].append(
                            {"from": fqcn, "to": tgt,
                             "looks_general_entry": sa["looks_general_entry"]})
                        queue.append((tgt, depth + 1))

        if chain["terminated_reason"] is None:
            if len(chain["nodes"]) >= max_nodes:
                chain["terminated_reason"] = "max_nodes"
            elif gate_on_policy:
                chain["terminated_reason"] = "no_policy_related_children"
            else:
                chain["terminated_reason"] = "hop_ceiling_reached"

    result["chain"] = chain

    if "resources" in layers:
        policy_strings = [{"name": k, "value": v} for k, v in strings.items()
                          if POLICY_REGEX.search(k) or POLICY_REGEX.search(v)]
    else:
        policy_strings = []
    policy_urls = sorted({u for n in chain["nodes"]
                          for u in n.get("behavior", {}).get("urls", [])
                          if POLICY_REGEX.search(u)})
    bundled = find_bundled_policy(app_root) if "bundled" in layers else []
    result["resources"] = {
        "policy_strings": policy_strings[:50],
        "policy_urls": policy_urls,
        "bundled_policy_files": bundled,
    }

    nodes = chain["nodes"]
    webview_nodes = [n for n in nodes if n.get("behavior", {}).get("webviews")]
    routes_general = any(e.get("looks_general_entry") for e in chain["edges"])
    routes_dedicated = any(n["policy_related"] and not n["is_rationale_activity"] for n in nodes)
    result["features"] = {
        "ra_declared": ra.get("declared", False),
        "ra_style": ra.get("style"),
        "ra_source_resolved": bool(ra.get("declared") and class_to_source_path(sources_dir, ra.get("implementation_class"))),
        "internet_permission": manifest["internet_permission"],
        "uses_cleartext_traffic": manifest["uses_cleartext_traffic"],
        "chain_node_count": len(nodes),
        "chain_max_depth": max((n["depth"] for n in nodes), default=-1),
        "has_webview_in_chain": bool(webview_nodes),
        "has_policy_url": bool(policy_urls) or any(wv["is_policy_url"] for n in nodes for wv in n.get("behavior", {}).get("webviews", [])),
        "has_cleartext_policy_url": any(wv["is_cleartext"] for n in nodes for wv in n.get("behavior", {}).get("webviews", [])),
        "has_local_policy_file_load": any(wv["is_local"] for n in nodes for wv in n.get("behavior", {}).get("webviews", [])),
        "has_dynamic_webview": any(wv["dynamic"] for n in nodes for wv in n.get("behavior", {}).get("webviews", [])),
        "routes_to_dedicated_policy_page": routes_dedicated,
        "routes_to_general_entry": routes_general,
        "bundled_policy_present": bool(bundled),
        "terminated_reason": chain["terminated_reason"],
    }
    return result


def build_text_bundle(res):
    """LLM-ready plain-text bundle from the structured result."""
    L = []
    f = res["features"]
    m = res["manifest"]
    L.append("PACKAGE: %s" % res["package"])
    L.append("RA_DECLARED: %s (style=%s)" % (f["ra_declared"], f.get("ra_style")))
    L.append("RA_CLASS: %s" % m["rationale_activity"].get("implementation_class"))
    L.append("HC_PERMISSIONS: %s" % ", ".join(m.get("hc_permissions", [])))
    L.append("INTERNET: %s  CLEARTEXT: %s" % (m["internet_permission"], m["uses_cleartext_traffic"]))
    L.append("TERMINATED: %s" % f["terminated_reason"])
    L.append("ROUTES_DEDICATED_POLICY: %s  ROUTES_GENERAL_ENTRY: %s" % (
        f["routes_to_dedicated_policy_page"], f["routes_to_general_entry"]))
    L.append("POLICY_URLS: %s" % ", ".join(res["resources"]["policy_urls"]))
    L.append("")
    L.append("=== NAVIGATION CHAIN ===")
    for n in res["chain"]["nodes"]:
        L.append("[depth %d] %s  policy_related=%s" % (
            n["depth"], n["class"], n.get("policy_related")))
        if n.get("policy_signals"):
            L.append("    signals: %s" % ", ".join(n["policy_signals"]))
        b = n.get("behavior", {})
        for wv in b.get("webviews", []):
            L.append("    webview.%s url=%s policy=%s cleartext=%s dynamic=%s" % (
                wv["method"], wv.get("url_literal"), wv["is_policy_url"],
                wv["is_cleartext"], wv["dynamic"]))
        for sa in b.get("start_activities", []):
            L.append("    -> startActivity %s (general_entry=%s)" % (
                sa.get("target"), sa.get("looks_general_entry")))
        if n.get("onCreate_excerpt"):
            L.append("    onCreate: %s" % n["onCreate_excerpt"].replace("\n", " ")[:400])
    if res["resources"]["bundled_policy_files"]:
        L.append("")
        L.append("=== BUNDLED POLICY FILES ===")
        for bf in res["resources"]["bundled_policy_files"]:
            L.append("    %s (%s bytes)" % (bf["path"], bf.get("size")))
    return "\n".join(L)


CSV_COLUMNS = [
    "package", "ra_declared", "ra_style", "ra_source_resolved",
    "internet_permission", "uses_cleartext_traffic", "chain_node_count",
    "chain_max_depth", "has_webview_in_chain", "has_policy_url",
    "has_cleartext_policy_url", "has_local_policy_file_load",
    "has_dynamic_webview", "routes_to_dedicated_policy_page",
    "routes_to_general_entry", "bundled_policy_present", "terminated_reason",
]


def run_locate_only(input_dir, packages, output_csv, quiet=False, verbose=False):
    """Authoritative RA-source location only. Writes a CSV and returns counts."""
    import csv as _csv
    rows = []
    found = 0
    not_found = 0
    no_ra = 0
    total = len(packages)
    for i, pkg in enumerate(packages, 1):
        # multi-root: search --input then EXTRA_SOURCE_ROOTS, matching the pipeline
        # (without this the 60 apps that live only in decompile_70 falsely report
        # app_folder_missing)
        app_root = resolve_app_root(input_dir, pkg)
        res_dir = os.path.join(app_root, "resources")
        sources_dir = os.path.join(app_root, "sources")
        manifest_path = os.path.join(res_dir, "AndroidManifest.xml")

        row = {"package": pkg, "ra_declared": False, "ra_class": "",
               "ra_style": "", "source_path": "", "locate_method": "",
               "status": "", "detail": ""}

        if not os.path.isdir(app_root):
            row["status"] = "app_folder_missing"
            row["detail"] = "app_folder_missing"
            rows.append(row); not_found += 1
        else:
            manifest = parse_manifest(manifest_path, pkg)
            ra = manifest["rationale_activity"]
            row["ra_declared"] = ra.get("declared", False)
            row["ra_class"] = ra.get("implementation_class", "") or ""
            row["ra_style"] = ra.get("style", "") or ""

            if not ra.get("declared"):
                row["status"] = "no_ra_declared"
                row["locate_method"] = "no_class"
                row["detail"] = "no_ra_declared"
                rows.append(row); no_ra += 1
            else:
                # try EACH candidate class until one resolves to a real file
                cands = ra.get("candidate_classes") or [ra.get("implementation_class")]
                path, method, matched = None, "not_found", None
                for cand in cands:
                    p, m = locate_ra_source(sources_dir, cand)
                    if p:
                        path, method, matched = p, m, cand
                        break
                if path:
                    row["ra_class"] = matched          # the class that actually resolved
                    row["source_path"] = path
                    row["locate_method"] = method
                    row["status"] = "located"
                    row["detail"] = "located"
                    found += 1
                else:
                    row["locate_method"] = "not_found"
                    row["status"] = "source_not_found"
                    # diagnostic-only sub-reason; does not change the not_found tally
                    row["detail"] = classify_unlocated_reason(sources_dir, ra)
                    not_found += 1
                rows.append(row)

        # ---- progress reporting ----
        if not quiet:
            if verbose:
                # one line per app (full detail)
                print("[%d/%d] %-45s %-16s %s" % (
                    i, total, pkg, row["status"], row["locate_method"]))
            else:
                # single overwriting status line + running tally
                bar_done = int(30 * i / total)
                bar = "#" * bar_done + "-" * (30 - bar_done)
                sys.stdout.write(
                    "\r[%s] %d/%d  located=%d  not_found=%d  no_ra=%d   " % (
                        bar, i, total, found, not_found, no_ra))
                sys.stdout.flush()
    if not quiet and not verbose:
        sys.stdout.write("\n")  # finish the progress line

    cols = ["package", "ra_declared", "ra_style", "ra_class",
            "source_path", "locate_method", "status", "detail"]
    with open(output_csv, "w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return {"total": len(rows), "located": found,
            "not_found": not_found, "no_ra_declared": no_ra}


def main():
    ap = argparse.ArgumentParser(
        description="HC privacy-policy RQ2 evidence extractor (configurable)")
    ap.add_argument("--input", required=True,
                    help="base dir containing <package>/ folders")
    ap.add_argument("--output", required=True,
                    help="dir to write output files")
    ap.add_argument("--package", help="optional: run a single package only")

    # (1) OUTPUT FORMAT
    ap.add_argument("--format", choices=["json", "text", "both", "csv"],
                    default="json",
                    help="json=per-app JSON (default); text=per-app LLM bundle; "
                         "both=JSON+text; csv=one feature table across all apps")

    # (2) CHAIN DEPTH MODE
    ap.add_argument("--chain", choices=["ra_only", "hop1", "hop2", "policy"],
                    default="policy",
                    help="ra_only=RA only; hop1/hop2=fixed hops; "
                         "policy=follow while policy-related (default)")
    ap.add_argument("--max-nodes", type=int, default=MAX_NODES,
                    help="safety cap on total nodes walked (default %d)" % MAX_NODES)
    ap.add_argument("--max-depth", type=int, default=MAX_DEPTH,
                    help="safety cap on hop depth in policy mode (default %d)" % MAX_DEPTH)

    # (3) EVIDENCE LAYERS
    ap.add_argument("--layers", default="manifest,java,resources,bundled",
                    help="comma list from: manifest,java,resources,bundled "
                         "(default all)")

    ap.add_argument("--locate-only", action="store_true",
                    help="only resolve RA source location, write a CSV summary")
    ap.add_argument("--verbose", action="store_true",
                    help="locate-only: print one line per app instead of a progress bar")
    ap.add_argument("--applist",
                    help="text file with one package per line (subset to process)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    layers = {x.strip() for x in args.layers.split(",") if x.strip()}
    valid = {"manifest", "java", "resources", "bundled"}
    bad = layers - valid
    if bad:
        ap.error("unknown layer(s): %s (valid: %s)" % (", ".join(bad), ", ".join(sorted(valid))))

    cfg = {
        "layers": layers,
        "chain_mode": args.chain,
        "max_nodes": args.max_nodes,
        "max_depth": args.max_depth,
    }

    os.makedirs(args.output, exist_ok=True)

    if args.package:
        pkgs = [args.package]
    else:
        pkgs = sorted(d for d in os.listdir(args.input)
                      if os.path.isdir(os.path.join(args.input, d)))

    if args.applist:
        raw = open(args.applist, encoding="utf-8-sig").read()
        wanted = [ln.strip().lstrip("\ufeff") for ln in raw.splitlines() if ln.strip()]
        pkgs = wanted

    if args.locate_only:
        out_csv = os.path.join(args.output, "ra_source_locations.csv")
        counts = run_locate_only(args.input, pkgs, out_csv, quiet=args.quiet, verbose=args.verbose)
        print("\n===== RA SOURCE LOCATION SUMMARY =====")
        print("Total apps        : %d" % counts["total"])
        print("Located           : %d" % counts["located"])
        print("Source NOT found  : %d" % counts["not_found"])
        print("No RA declared    : %d" % counts["no_ra_declared"])
        print("\nCSV written: %s" % out_csv)
        return

    total = len(pkgs)
    ok = 0
    csv_rows = []
    for i, pkg in enumerate(pkgs, 1):
        app_root = os.path.join(args.input, pkg)
        try:
            res = extract_app(app_root, pkg, cfg)

            if args.format in ("json", "both"):
                with open(os.path.join(args.output, pkg + ".json"), "w",
                          encoding="utf-8") as fh:
                    json.dump(res, fh, indent=2, ensure_ascii=False)
            if args.format in ("text", "both"):
                with open(os.path.join(args.output, pkg + ".txt"), "w",
                          encoding="utf-8") as fh:
                    fh.write(build_text_bundle(res))
            if args.format == "csv":
                row = dict(res["features"])
                row["package"] = res["package"]
                csv_rows.append(row)

            ok += 1
            if not args.quiet:
                fdict = res["features"]
                print("[%d/%d] %s  ra=%s  nodes=%s  term=%s" % (
                    i, total, pkg, fdict["ra_declared"],
                    fdict["chain_node_count"], fdict["terminated_reason"]))
        except Exception as e:
            if not args.quiet:
                print("[%d/%d] %s  ERROR: %s" % (i, total, pkg, e), file=sys.stderr)

    if args.format == "csv":
        import csv
        out_csv = os.path.join(args.output, "rq2_features.csv")
        with open(out_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            w.writeheader()
            for row in csv_rows:
                w.writerow(row)
        print("\nWrote feature table: %s (%d rows)" % (out_csv, len(csv_rows)))

    print("\nDone: %d/%d apps extracted -> %s" % (ok, total, args.output))


if __name__ == "__main__":
    main()
