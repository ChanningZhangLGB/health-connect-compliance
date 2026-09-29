# Case 05 — Baseline + RA-RAG — Gemini-2.5-flash — `digifit.virtuagym.client.android` (Virtuagym)

| | |
|---|---|
| **Model** | Gemini-2.5-flash |
| **App / package** | `digifit.virtuagym.client.android` |
| **Ground truth** | **COMPLIANT** (`compliance = Yes` — the privacy policy *is* displayed) |
| **Baseline verdict** | ❌ `Answer1 = "No"` → predicted **VIOLATION** (WRONG) |
| **Baseline + RA-RAG verdict** | ✅ `Answer1 = "Yes"` → predicted **COMPLIANT** (RIGHT) |
| **Retrieved reference** | 1 lesson, `[support: 4]`, injected because a similar RA (SpotMe) exceeded the cosine-similarity threshold |

> **What "RA-RAG" is here:** the `baseline-online` pipeline runs the *same single-pass baseline
> analyst*, but first retrieves a "blunder book" lesson whenever the current app's rationale-activity
> (RA) source embedding is cosine-similar (≥ threshold) to a past app that a lesson was drawn from.
> The lesson text is injected into the prompt as a `BLUNDER BOOK (reference)` block.
>
> **Why this is a clean causal test:** the baseline prompt and the RA-RAG prompt below are
> **byte-for-byte identical except for the injected reference block**. Same model, same
> temperature, same code — the only added variable is the retrieved lesson, and the answer flips
> from wrong to right. This isolates the reference as the cause of the correction.

---

## The RA code (identical in both runs)

```java
public final class PrivacyPolicyActivity extends BaseActivity {
    @Inject public PrimaryColor a;

    @Override
    public final void onCreate(@Nullable Bundle bundle) {
        super.onCreate(bundle);
        Injector.Companion.a(this).D0(this);
        String string = getString(R.string.club_terms_and_conditions_url);
        if (StringsKt.y(string)) {
            string = getString(R.string.privacy_statement_url);   // privacy policy URL
        }
        String string2 = getString(R.string.privacy_statement);   // title
        // Jetpack Compose: render the privacy-policy screen with the URL + title
        ComponentActivityKt.setContent$default(this, null,
            ComposableLambdaKt.composableLambdaInstance(-1210448063, true, new Function2<>() {
                public final Unit invoke(Composer composer, Integer num) {
                    ...
                    PrivacyPolicyScreenKt.a(string, string2, ColorKt.Color(primaryColor.a()), composer, 0);
                    ...
                }
            }), 1, null);
    }
}
```

The policy is rendered by a Jetpack **Compose** screen (`PrivacyPolicyScreenKt.a(url, title, color, …)`)
inside `setContent { … }` — there is no classic `WebView`/`Intent`, so the display is easy to miss.

---

## Baseline (no reference) — WRONG

```json
{
  "Answer1": "No",
  "Answer2": ""
}
```

Seeing no explicit `Intent`/`WebView` and only an opaque Compose call, the baseline concludes
"No" — a **false-positive violation**.

---

## Baseline + RA-RAG (with retrieved reference) — RIGHT

### The injected reference block (the *only* difference from the baseline prompt)

```
===================== BLUNDER BOOK (reference) =====================
[support: 4] When analyzing for privacy policy display, do not solely rely on the absence of a
direct code trace to a privacy policy activity. The ground truth indicates compliance, and the
provided image shows a webpage titled 'Privacy Notice for SpotMe Services' ... This strongly
suggests that the app *does* display its privacy policy, even if the analyst's code trace did not
find the specific activity or intent that launches it. The analyst should have considered the
presence of the privacy policy webpage itself as evidence of compliance ... rather than concluding
'No' based on a lack of a direct code path.
=================== end of BLUNDER BOOK ===================
```

### RA-RAG response

```json
{
  "Answer1": "Yes",
  "Answer2": "The `PrivacyPolicyActivity` is responsible for displaying the app's privacy policy. It retrieves URLs for terms and conditions and privacy statements from resources. The `onCreate` method sets the content using Jetpack Compose, calling `PrivacyPolicyScreenKt.a` which likely renders the privacy policy content using the provided URLs and primary color."
}
```

---

## Why the retrieved reference is responsible

The lesson — distilled from a *previous* mistake on the SpotMe app — states the exact anti-pattern
the baseline just committed: **"do not … rely on the absence of a direct code trace … rather than
concluding 'No' based on a lack of a direct code path."** It was retrieved specifically because
Virtuagym's RA embedding is cosine-similar to SpotMe's.

With that guidance in the prompt, the analyst stops treating "no explicit `Intent`/`WebView`" as
"No", re-weighs the evidence (the resolved `privacy_statement_url` + the Compose
`PrivacyPolicyScreenKt.a(...)` render), and returns the correct **COMPLIANT** verdict. Because the
code and every other prompt token are unchanged, the injected reference is the sole cause of the flip.
