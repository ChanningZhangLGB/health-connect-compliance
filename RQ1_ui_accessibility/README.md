# RQ1 — UI accessibility of the Health Connect privacy policy

**Question:** when a user taps *"Read privacy policy"* on an app's Health Connect (HC)
permissions screen, does the app actually display a privacy policy? RQ1 answers this
**dynamically** by driving a real device/emulator with ADB and screenshotting the result.

## What the script does

`scripts/hc_ui_accessibility_test.ps1` automates, per app:

1. Open the Play Store page and **install** the app.
2. (optional) pull its `base.apk`.
3. Open **Health Connect → App permissions → <app>** (under "Not allowed access").
4. Scroll to find the **"Read privacy policy"** link and tap it.
5. Wait for the page to load, dismiss any system dialog, and **screenshot** it as
   `screen_<package>.png`.
6. Go back and **uninstall** the app.

The screenshot is the evidence a human then labels as *policy shown* vs *violation*.

## Requirements

- **Android device or emulator** with Health Connect installed, signed into the Play
  Store, connected over ADB. Verify with `adb devices` (exactly one `device`).
- **Android platform-tools** (`adb`) on your `PATH` (or pass `-AdbPath`).
- **Windows PowerShell 5.1+** (the script is PowerShell).

## Usage

```powershell
# 20-app sample (defaults: sample_apps/sample_apps_20.txt -> ../output_screenshots)
powershell -ExecutionPolicy Bypass -File .\scripts\hc_ui_accessibility_test.ps1

# a single app
.\scripts\hc_ui_accessibility_test.ps1 -Package com.resmed.myair

# custom list, output dir, and also pull APKs
.\scripts\hc_ui_accessibility_test.ps1 `
    -PackageListFile .\sample_apps\sample_apps_20.txt `
    -ScreenshotDir .\output_screenshots `
    -ApkDir .\apks
```

Key parameters (all optional, see the script header): `-AdbPath`, `-ScreenshotWait`,
`-MaxScrolls`, `-InstallMaxSec`, `-DialogSettle`. Runs are **resumable** — apps whose
`screen_<package>.png` already exists are skipped.

## What's in this folder

| path | description |
|---|---|
| `scripts/hc_ui_accessibility_test.ps1` | the parameterized UI-accessibility driver |
| `sample_apps/sample_apps_20.txt` | 20-app sample input (10 compliant + 10 violation) |
| `sample_output_screenshots/` | the 20 expected result screenshots from the paper's run |

Compare a fresh run in `output_screenshots/` against `sample_output_screenshots/` to
sanity-check your setup. Labels for these 20 apps are in
`../data/sample_ground_truth_20.csv` (`compliance` = Yes → policy shown).
