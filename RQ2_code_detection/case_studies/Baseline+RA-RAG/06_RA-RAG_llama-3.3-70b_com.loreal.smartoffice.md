# Case 06 — Baseline + RA-RAG — Llama-3.3-70B — `com.loreal.smartoffice`

| | |
|---|---|
| **Model** | Llama-3.3-70B |
| **App / package** | `com.loreal.smartoffice` |
| **Ground truth** | **VIOLATION** (`compliance = No` — no privacy policy is displayed) |
| **Baseline verdict** | ❌ `Answer1 = "Yes"` → predicted **COMPLIANT** (WRONG) |
| **Baseline + RA-RAG verdict** | ✅ `Answer1 = "No"` → predicted **VIOLATION** (RIGHT) |
| **Retrieved reference** | 1 lesson, `[support: 24]`, injected via RA-source cosine similarity to prior mistaken apps |

> **What "RA-RAG" is here:** the `baseline-online` pipeline runs the *same single-pass baseline
> analyst*, but injects a retrieved "blunder book" lesson into the prompt when the current app's
> rationale-activity (RA) source embedding is cosine-similar (≥ threshold) to a past app a lesson
> was drawn from.
>
> **Clean causal test:** the baseline prompt and the RA-RAG prompt below are **identical except for
> the injected reference block**. Same model, same code — the answer flips wrong→right, isolating
> the reference as the cause.

---

## The RA code (identical in both runs)

```java
public final class PermissionsRationaleActivity extends c {
    public static final class a extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView webView, WebResourceRequest webResourceRequest) {
            return false;
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        WebView webView = new WebView(this);
        webView.setWebViewClient(new a());
        // NOT a privacy policy — this is Google's Health Connect *developer* guide:
        webView.loadUrl("https://developer.android.com/health-and-fitness/guides/health-connect/develop/get-started");
        setContentView(webView);
    }
}
```

The RA does open a `WebView`, but the URL it loads is the **Health Connect developer "get-started"
guide** — not the app's privacy policy. So the app fails to show a real policy → **VIOLATION**.

---

## Baseline (no reference) — WRONG

```json
{
  "Answer1": "Yes",
  "Answer2": "This activity creates a WebView and loads a URL from the Android developer website, specifically the Health Connect get-started guide, when the activity is created. It overrides the URL loading to prevent external apps from handling the URL, keeping the user within the app."
}
```

The baseline pattern-matches "`WebView` + `loadUrl(...)`" to "displays the privacy policy" and
answers **"Yes"** — even though its *own* `Answer2` admits the URL is the *get-started guide*, not a
policy. This is a **missed violation** (false negative).

---

## Baseline + RA-RAG (with retrieved reference) — RIGHT

### The injected reference block (the *only* difference from the baseline prompt)

```
===================== BLUNDER BOOK (reference) =====================
[support: 24] When determining if an app implements a specific functionality, such as displaying a
privacy policy, the analyst should thoroughly examine the app's code for relevant keywords, intent
filters, and activity declarations, rather than relying solely on the absence of explicit evidence.
This includes checking for nested activities, fragments, or web views that might be used to display
the required information. By doing so, the analyst can make a more informed decision and avoid
incorrect verdicts.
=================== end of BLUNDER BOOK ===================
```

### RA-RAG response

```json
{
  "Answer1": "No",
  "Answer2": ""
}
```

---

## Why the retrieved reference is responsible

The lesson (reinforced across **24** prior apps) tells the analyst to *thoroughly examine what a
web view actually displays* — "checking for … web views that might be used to display **the required
information**" — instead of making a verdict from a surface pattern. Applied here, that pushes the
model past the naive "`WebView` ⇒ policy" shortcut to actually judge the loaded URL: it is the
Health Connect *developer guide*, not a privacy policy. The model therefore returns the correct
**VIOLATION** verdict.

Because the code and every other prompt token are identical to the baseline run, the injected
reference is the sole cause of the flip from wrong ("Yes") to right ("No").

*(The same reference corrects the identical false positive across a family of apps that ship this
exact RA — e.g. `app.planks.workout`, `app.workout.movebody`, `com.codingmachine`,
`com.lebouging.app` — all baseline "Yes" → RA-RAG "No".)*
