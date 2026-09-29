# Reconstructed decompiled sample apps (LLM smoke test)

The `llm_agentic` pipeline reads **JADX-decompiled APK source trees**, one folder
per package, in this layout:

```
<package>/resources/AndroidManifest.xml     # declares the HC rationale activity (RA)
<package>/sources/<pkg/path>/*.java         # the RA class + everything it references
```

The full study pointed `--input` at a corpus of ~673 decompiled apps (too large to
ship). The two apps here are **faithful minimal reconstructions** (real source of the
RA class + the one helper it delegates to) so you can run the pipeline end-to-end on a
couple of apps without the full corpus:

| package | ground truth | why |
|---|---|---|
| `com.bosch.ebike`    | COMPLIANT | RA delegates to helper `E2.I`, whose `g()` builds the PrivacyPolicy URL and `p()` opens it in a Custom Tab. Baseline (RA only) tends to say "No"; RA-ReAct follows `E2.I` and says "Yes". |
| `app.planks.workout` | VIOLATION | RA loads a `WebView`, but the URL is Google's Health Connect *developer guide*, not a privacy policy. |

To produce your own inputs, decompile APKs with [JADX](https://github.com/skylot/jadx):

```
jadx -d <out>/<package> <package>.apk      # yields <out>/<package>/{resources,sources}
```

Smoke test that the RA locator sees them (no API key needed):

```
python ../utils/hc_extractor.py --input . --applist sample_apps_2.txt \
       --output _locate_out --locate-only --verbose
```
