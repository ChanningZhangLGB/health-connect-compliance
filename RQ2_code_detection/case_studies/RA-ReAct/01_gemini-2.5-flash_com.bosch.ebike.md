# Case 01 — Gemini-2.5-flash — `com.bosch.ebike` (Bosch eBike Flow)

| | |
|---|---|
| **Model** | Gemini-2.5-flash |
| **App / package** | `com.bosch.ebike` |
| **Ground truth** | **COMPLIANT** (`compliance = Yes` — the privacy policy *is* displayed) |
| **Baseline verdict** | ❌ `Answer1 = "No"` → predicted **VIOLATION** (WRONG) |
| **RA-ReAct verdict** | ✅ `Answer1 = "Yes"` → predicted **COMPLIANT** (RIGHT) |
| **RA-ReAct hops** | 2 (`hop0` → follow `E2.I` → `hop1`) |

**One-line story:** the rationale activity hands the work to an opaque helper class `E2.I`.
Baseline can't see inside it and answers "No"; RA-ReAct follows the reference into `E2.I`,
finds `g()` builds the *PrivacyPolicy* URL and `p()` opens it in a Chrome Custom Tab, and
answers "Yes".

---

## Baseline (single pass) — WRONG

### Input code (the RA activity, in isolation)

```java
package com.bosch.ebike.app.ui.activities;

import E2.I;
import android.os.Bundle;
import z2.AbstractActivityC2451a;

public final class PermissionsRationaleActivity extends AbstractActivityC2451a {
    @Override
    protected void onCreate(Bundle bundle) {
        super.onCreate(bundle);
        I.p(getApplicationContext(), I.g(getApplicationContext()));   // <-- opaque helper calls
        finish();
    }
}
```

### Baseline response

```json
{
  "Answer1": "No",
  "Answer2": ""
}
```

The activity only calls `I.p(context, I.g(context))`. With no visibility into class `I`,
the baseline cannot confirm a privacy-policy display and defaults to **"No" (violation)** —
incorrect.

---

## RA-ReAct (multi-hop) — RIGHT

### hop 0 — sees the same RA activity, recognises the delegation

```json
{
  "Answer1": "No",
  "Answer2": "",
  "Decision": "CONTINUE",
  "next_target": "E2.I"
}
```

Instead of concluding "No", the model asks the harness to retrieve class `E2.I`.

### hop 1 — after `E2.I` is retrieved and appended

Key retrieved code (`E2/I.java`):

```java
public abstract class I {
    // builds the privacy-policy URL
    public static String g(Context context) {
        return n(context, "/documents/archive/current/PrivacyPolicy");
    }
    // opens the given URL in a Chrome Custom Tab
    public static void p(Context context, String str) {
        ...
        androidx.browser.customtabs.b a8 = c0158b.a();
        a8.a(context, Uri.parse(str));   // <-- displays the policy
    }
}
```

### RA-ReAct final response

```json
{
  "Answer1": "Yes",
  "Answer2": "The `PermissionsRationaleActivity` calls `I.p(getApplicationContext(), I.g(getApplicationContext()))` in its `onCreate` method. The `I.g(context)` method constructs a URL for the privacy policy, and `I.p(context, url)` opens this URL in a Custom Tab. This activity is likely triggered when a user needs to review permissions and is presented with a link to the privacy policy.",
  "Decision": "YES",
  "next_target": ""
}
```

---

## Why RA-ReAct wins

The privacy-policy display is **one indirection away** from the rationale activity:
`I.g()` produces the `.../PrivacyPolicy` URL and `I.p()` renders it in a Custom Tab.
The baseline sees only the call site `I.p(..., I.g(...))` and, lacking the helper body,
falls back to "No" — a false-positive violation. RA-ReAct's `CONTINUE → E2.I` retrieval
step surfaces the helper, letting the model verify the display and return the correct
**COMPLIANT** verdict.
