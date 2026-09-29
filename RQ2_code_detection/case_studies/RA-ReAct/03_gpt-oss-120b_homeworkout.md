# Case 03 — GPT-oss-120b — `homeworkout.homeworkouts.noequipment` (Home Workout – No Equipment)

| | |
|---|---|
| **Model** | GPT-oss-120b |
| **App / package** | `homeworkout.homeworkouts.noequipment` |
| **Ground truth** | **COMPLIANT** (`compliance = Yes` — the privacy policy *is* displayed) |
| **Baseline verdict** | ❌ `Answer1 = "No"` → predicted **VIOLATION** (WRONG) |
| **RA-ReAct verdict** | ✅ `Answer1 = "Yes"` → predicted **COMPLIANT** (RIGHT) |
| **RA-ReAct hops** | 2 (`hop0` → follow `bu.h` → `hop1`) |

**One-line story:** the rationale activity is a one-line shell that calls a static helper
`bu.h.a(this)`. Baseline sees the empty shell and answers "No"; RA-ReAct follows `bu.h`,
finds the helper decodes an (obfuscated) policy URL and opens it, and answers "Yes".

---

## Baseline (single pass) — WRONG

### Input code (the RA activity, in isolation)

```java
package homeworkout.homeworkouts.noequipment;

import android.os.Bundle;

public final class RationaleActivity extends androidx.appcompat.app.e {
    @Override
    public void onCreate(Bundle bundle) {
        super.onCreate(bundle);
        bu.h.a(this);   // <-- everything is delegated here
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

The activity body is a single delegated call `bu.h.a(this)`. With no view of `bu.h`, the
baseline defaults to **"No" (violation)** — incorrect.

---

## RA-ReAct (multi-hop) — RIGHT

### hop 0 — recognises the delegation, requests the helper

```json
{
  "Answer1": "No",
  "Answer2": "",
  "Decision": "CONTINUE",
  "next_target": "bu.h"
}
```

### hop 1 — after `bu.h` is retrieved and appended

Key retrieved code (`bu/h.java`, a `SettingHelper`; strings are runtime-deobfuscated by
`bj.j.a(...)`):

```java
public final class h {
    public static final void a(Activity activity) {
        kw.m.f(activity, bj.j.a("B2MZaT1pHXk=", "UFWKzwjN"));
        ur.a.c(activity,
               activity.getString(R.string.arg),
               Color.parseColor(bj.j.a("GzEbMnAyYw==", "0SFPSbSm")),
               bj.j.a("CG8fdCNwCHIFLg9uE3IcaS5ADW0NaSIuBm9t", "E3CVeBth"));  // decoded URL
    }
    // b(...) builds/pushes a dialog fragment (v0) carrying title/url ...
}
```

### RA-ReAct final response

```json
{
  "Answer1": "Yes",
  "Answer2": "RationaleActivity is launched (likely from the Health Connect permission screen). In its onCreate it immediately calls bu.h.a(this) and then finishes. The static method bu.h.a(Activity) decodes an obfuscated string (presumably a URL) and passes it to kw.m.f, which is very likely a helper that opens the URL (e.g., in a browser or WebView). It also calls ur.a.c to possibly show a styled dialog with the policy text. Thus the activity delegates the actual privacy-policy display to the helper class, but it is the entry point that triggers the display when the user clicks the link.",
  "Decision": "YES",
  "next_target": ""
}
```

---

## Why RA-ReAct wins

The rationale activity is intentionally thin — the display logic lives entirely in the
`bu.h` helper, and the target URL is obfuscated. Baseline has nothing to reason about and
emits a false-positive violation. RA-ReAct's `CONTINUE → bu.h` retrieval exposes the helper
that decodes the policy URL and opens it (with a styled dialog fallback), letting the model
confirm the display and return the correct **COMPLIANT** verdict.
