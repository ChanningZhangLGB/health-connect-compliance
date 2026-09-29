<#
.SYNOPSIS
    RQ1 - Health Connect "Read privacy policy" UI-accessibility test (dynamic).

    For each package it: opens the Play Store page and installs the app, pulls the
    base.apk (optional), opens the Health Connect permission screen for the app,
    scrolls to find the "Read privacy policy" link, taps it, screenshots whatever
    is displayed, then uninstalls. The screenshot is the ground-truth evidence of
    whether the privacy policy is actually reachable/shown at runtime.

.REQUIREMENTS
    - Android device or emulator with Health Connect installed, connected via ADB
      (run `adb devices` and confirm one "device").
    - Android platform-tools (adb) on PATH, or pass -AdbPath.
    - You must be signed into the Play Store on the device (for install/uninstall).

.USAGE
    # 20-app sample, defaults resolve to the repo's sample list + output folders:
    powershell -ExecutionPolicy Bypass -File .\hc_ui_accessibility_test.ps1

    # a single app:
    .\hc_ui_accessibility_test.ps1 -Package com.resmed.myair

    # a custom list and output dir:
    .\hc_ui_accessibility_test.ps1 -PackageListFile my_apps.txt -ScreenshotDir out\shots

.NOTES
    Screenshots are written as screen_<package>.png. Existing screenshots are
    skipped so the batch is resumable. UI coordinates/labels target a standard
    Health Connect layout; adjust the fallback taps for unusual screen sizes.
#>
param(
    [string]$Package,                                   # single app (overrides list)
    [string]$PackageListFile = (Join-Path $PSScriptRoot "..\sample_apps\sample_apps_20.txt"),
    [string]$ScreenshotDir   = (Join-Path $PSScriptRoot "..\output_screenshots"),
    [string]$DumpDir         = (Join-Path $PSScriptRoot "..\_dump"),
    [string]$ApkDir,                                    # optional: pull base.apk here
    [string]$AdbPath = "adb",                           # adb on PATH by default

    [int]$PostLaunchWait = 1,
    [int]$InstallPollSec = 5,
    [int]$InstallMaxSec  = 120,
    [int]$ScreenshotWait = 11,
    [int]$MaxScrolls     = 10,
    [int]$DialogSettle   = 5
)

$Adb = $AdbPath
New-Item -ItemType Directory -Force $ScreenshotDir | Out-Null
New-Item -ItemType Directory -Force $DumpDir | Out-Null

# ---- auto-detect device serial ----
$deviceSerial = (& $Adb devices | Select-String -Pattern "^\S+\s+device$" | Select-Object -First 1).Line.Split("`t")[0].Trim()
if (-not $deviceSerial) {
    Write-Host "ERROR: No ADB device found. Connect a device/emulator and run 'adb devices'."
    exit 1
}
Write-Host "Using device: $deviceSerial"
if ($ApkDir) { New-Item -ItemType Directory -Force $ApkDir | Out-Null }

function Get-Center([System.Xml.XmlNode]$node) {
    $b = $node.GetAttribute("bounds")
    if ($b -match '\[(\d+),(\d+)\]\[(\d+),(\d+)\]') {
        return @{ X = [int](([int]$Matches[1]+[int]$Matches[3])/2)
                  Y = [int](([int]$Matches[2]+[int]$Matches[4])/2) }
    }
    return $null
}

function Safe-XPath([string]$text) {
    if ($text -notmatch "'") { return "contains(@text,'$text')" }
    $parts = $text -split "'" | ForEach-Object { "'$_'" }
    $concatArgs = ($parts -join ", `"'`", ")
    return "contains(@text,concat($concatArgs))"
}

function Is-AppInstalled([xml]$doc) {
    return ($doc.SelectSingleNode("//node[contains(@content-desc,'Installed')]") -ne $null)
}
function Is-InstallComplete([xml]$doc) {
    $installed = $doc.SelectSingleNode("//node[contains(@content-desc,'Installed')]")
    $uninstall = $doc.SelectSingleNode("//node[contains(@text,'Uninstall')]")
    return ($installed -ne $null -or $uninstall -ne $null)
}

function Pull-Apk([string]$pkg) {
    if (-not $ApkDir) { return }
    $basePath = & $Adb -s $deviceSerial shell pm path $pkg |
                ForEach-Object { $_ -replace "package:", "" } |
                ForEach-Object { $_.Trim() } |
                Where-Object { $_ -match "base\.apk$" } |
                Select-Object -First 1
    if (-not $basePath) { Write-Host "   WARNING: no base.apk path for $pkg"; return }
    $localPath = Join-Path $ApkDir "$pkg.apk"
    & $Adb -s $deviceSerial pull $basePath $localPath | Out-Null
    Write-Host "   pulled: $pkg.apk"
}

function Dismiss-SystemDialog {
    & $Adb -s $deviceSerial shell uiautomator dump /sdcard/dialog_dump.xml 2>$null
    & $Adb -s $deviceSerial pull /sdcard/dialog_dump.xml "$DumpDir\dialog_dump.xml" 2>$null | Out-Null
    if (-not (Test-Path "$DumpDir\dialog_dump.xml")) { return $false }
    [xml]$doc = Get-Content "$DumpDir\dialog_dump.xml" -Raw -ErrorAction SilentlyContinue
    if (-not $doc) { return $false }
    foreach ($text in @("Allow","Got it","OK","Continue","Skip","Not now","Don't allow")) {
        $xpath = "//node[" + (Safe-XPath $text) + " and @clickable='true']"
        try { $btn = $doc.SelectSingleNode($xpath) } catch { continue }
        if ($btn) {
            $c = Get-Center $btn
            if ($c) {
                & $Adb -s $deviceSerial shell input tap $c.X $c.Y
                Start-Sleep -Seconds $DialogSettle
                return $true
            }
        }
    }
    return $false
}

function Uninstall-Package([string]$pkg) {
    & $Adb -s $deviceSerial shell am start -a android.intent.action.VIEW -d "market://details?id=$pkg" | Out-Null
    Start-Sleep -Seconds $PostLaunchWait
    & $Adb -s $deviceSerial shell uiautomator dump /sdcard/uninstall_dump.xml | Out-Null
    & $Adb -s $deviceSerial pull /sdcard/uninstall_dump.xml "$DumpDir\uninstall_dump.xml" | Out-Null
    [xml]$doc = Get-Content "$DumpDir\uninstall_dump.xml" -Raw -ErrorAction SilentlyContinue
    $uNode = if ($doc) { $doc.SelectSingleNode("//node[contains(@text,'Uninstall')]") } else { $null }
    if ($uNode) {
        $c = Get-Center $uNode
        if ($c) { & $Adb -s $deviceSerial shell input tap $c.X $c.Y }
        Start-Sleep -Seconds 1
        & $Adb -s $deviceSerial shell uiautomator dump /sdcard/confirm_dump.xml | Out-Null
        & $Adb -s $deviceSerial pull /sdcard/confirm_dump.xml "$DumpDir\confirm_dump.xml" | Out-Null
        [xml]$cDoc = Get-Content "$DumpDir\confirm_dump.xml" -Raw -ErrorAction SilentlyContinue
        $confirmNode = $null
        if ($cDoc) {
            $confirmNode = $cDoc.SelectSingleNode("//node[contains(@text,'Uninstall') and @clickable='true']")
            if (-not $confirmNode) { $confirmNode = $cDoc.SelectSingleNode("//node[contains(@text,'OK')]") }
        }
        if ($confirmNode) { $c = Get-Center $confirmNode; if ($c) { & $Adb -s $deviceSerial shell input tap $c.X $c.Y } }
        else { & $Adb -s $deviceSerial shell input tap 840 1350 }
        Write-Host "   uninstalled via Play Store UI"
    } else {
        & $Adb -s $deviceSerial shell pm uninstall --user 0 $pkg | Out-Null
        Write-Host "   uninstalled via pm uninstall --user 0"
    }
}

# ============================================================
# MAIN LOOP
# ============================================================
if ($Package) { $packages = @($Package) }
else { $packages = Get-Content $PackageListFile | Where-Object { $_.Trim() -ne "" } }
$total = $packages.Count
$current = 0

foreach ($package in $packages) {
    $package = $package.Trim()
    $current++
    Write-Host "`n[$current/$total] === $package ==="

    if (Test-Path "$ScreenshotDir\screen_$package.png") { Write-Host "   already done - skipping"; continue }

    # Step 1: open Play Store & install
    & $Adb -s $deviceSerial shell am start -a android.intent.action.VIEW -d "market://details?id=$package" | Out-Null
    Start-Sleep -Seconds $PostLaunchWait
    & $Adb -s $deviceSerial shell uiautomator dump /sdcard/install_dump.xml | Out-Null
    & $Adb -s $deviceSerial pull /sdcard/install_dump.xml "$DumpDir\install_dump.xml" | Out-Null
    [xml]$doc = Get-Content "$DumpDir\install_dump.xml" -Raw
    $installNode = $doc.SelectSingleNode("//node[contains(@text,'Install')]")

    if (Is-AppInstalled $doc) {
        Write-Host "   already installed - proceeding"
    } elseif ($installNode) {
        $c = Get-Center $installNode
        if ($c) { & $Adb -s $deviceSerial shell input tap $c.X $c.Y }
        Write-Host "   tapped Install - polling..."
        Start-Sleep -Seconds 3
        $deadline = (Get-Date).AddSeconds($InstallMaxSec); $installed = $false
        do {
            Start-Sleep -Seconds $InstallPollSec
            & $Adb -s $deviceSerial shell uiautomator dump /sdcard/poll_dump.xml 2>$null
            & $Adb -s $deviceSerial pull /sdcard/poll_dump.xml "$DumpDir\poll_dump.xml" 2>$null | Out-Null
            if (Test-Path "$DumpDir\poll_dump.xml") {
                [xml]$pollDoc = Get-Content "$DumpDir\poll_dump.xml" -Raw -ErrorAction SilentlyContinue
                if ($pollDoc -and (Is-InstallComplete $pollDoc)) { $installed = $true; Write-Host "   install confirmed" }
            }
        } while (-not $installed -and (Get-Date) -lt $deadline)
        if (-not $installed) {
            Write-Host "   install timed out - screenshot and skip"
            & $Adb -s $deviceSerial shell screencap -p "/sdcard/screen_$package.png"
            & $Adb -s $deviceSerial pull "/sdcard/screen_$package.png" "$ScreenshotDir\screen_$package.png" | Out-Null
            continue
        }
    } else {
        Write-Host "   no Install button - screenshot and skip"
        & $Adb -s $deviceSerial shell screencap -p "/sdcard/screen_$package.png"
        & $Adb -s $deviceSerial pull "/sdcard/screen_$package.png" "$ScreenshotDir\screen_$package.png" | Out-Null
        continue
    }

    Pull-Apk $package

    # Step 2: open Health Connect app-info -> Open
    & $Adb -s $deviceSerial shell am start -a android.settings.APPLICATION_DETAILS_SETTINGS -d "package:com.google.android.apps.healthdata" | Out-Null
    Start-Sleep -Seconds $PostLaunchWait
    & $Adb -s $deviceSerial shell uiautomator dump /sdcard/open_dump.xml | Out-Null
    & $Adb -s $deviceSerial pull /sdcard/open_dump.xml "$DumpDir\open_dump.xml" | Out-Null
    [xml]$doc = Get-Content "$DumpDir\open_dump.xml" -Raw
    $openNode = $doc.SelectSingleNode("//node[contains(@text,'Open')]")
    if ($openNode) { $c = Get-Center $openNode; if ($c) { & $Adb -s $deviceSerial shell input tap $c.X $c.Y } }
    else { & $Adb -s $deviceSerial shell input tap 950 180 }
    Start-Sleep -Seconds $PostLaunchWait

    # Step 3-4: App permissions
    & $Adb -s $deviceSerial shell uiautomator dump /sdcard/ap_dump.xml | Out-Null
    & $Adb -s $deviceSerial pull /sdcard/ap_dump.xml "$DumpDir\ap_dump.xml" | Out-Null
    Start-Sleep -Seconds $PostLaunchWait
    [xml]$doc = Get-Content "$DumpDir\ap_dump.xml" -Raw
    $node = $doc.SelectSingleNode("//node[contains(@text,'App permissions')]")
    if ($node) { $c = Get-Center $node; if ($c) { & $Adb -s $deviceSerial shell input tap $c.X $c.Y } }
    Start-Sleep -Seconds $PostLaunchWait

    # Step 5: tap app under "Not allowed access"
    & $Adb -s $deviceSerial shell uiautomator dump /sdcard/ca_dump.xml | Out-Null
    & $Adb -s $deviceSerial pull /sdcard/ca_dump.xml "$DumpDir\ca_dump.xml" | Out-Null
    [xml]$doc = Get-Content "$DumpDir\ca_dump.xml" -Raw
    $naNode = $doc.SelectSingleNode("//node[contains(@text,'Not allowed access')]")
    if (-not $naNode) {
        Write-Host "   no 'Not allowed access' - screenshot and skip"
        & $Adb -s $deviceSerial shell screencap -p "/sdcard/screen_$package.png"
        & $Adb -s $deviceSerial pull "/sdcard/screen_$package.png" "$ScreenshotDir\screen_$package.png" | Out-Null
        & $Adb -s $deviceSerial shell am force-stop com.google.android.healthconnect.controller
        Uninstall-Package $package
        continue
    }
    $nextSibling = $naNode.NextSibling
    if ($nextSibling) { $c = Get-Center $nextSibling; if ($c) { & $Adb -s $deviceSerial shell input tap $c.X $c.Y } }
    else { $c = Get-Center $naNode; if ($c) { & $Adb -s $deviceSerial shell input tap $c.X ($c.Y + 100) } }
    Start-Sleep -Seconds $PostLaunchWait

    # Step 6-8: find "Read privacy policy", scrolling if needed
    Write-Host "   looking for 'Read privacy policy'..."
    $ppNode = $null; $scrolls = 0
    do {
        & $Adb -s $deviceSerial shell uiautomator dump /sdcard/rpp_dump.xml | Out-Null
        & $Adb -s $deviceSerial pull /sdcard/rpp_dump.xml "$DumpDir\rpp_dump.xml" | Out-Null
        [xml]$doc = Get-Content "$DumpDir\rpp_dump.xml" -Raw
        $ppNode = $doc.SelectSingleNode("//node[contains(@text,'Read privacy policy')]")
        if ($ppNode) { Write-Host "   found after $scrolls scroll(s)" }
        elseif ($scrolls -ge $MaxScrolls) { Write-Host "   not found after $MaxScrolls scrolls"; break }
        else { & $Adb -s $deviceSerial shell input swipe 500 1800 500 600 500; Start-Sleep -Seconds 1; $scrolls++ }
    } while (-not $ppNode)

    if ($ppNode) { $c = Get-Center $ppNode; if ($c) { & $Adb -s $deviceSerial shell input tap $c.X $c.Y } }
    else {
        $xmlContent = Get-Content "$DumpDir\rpp_dump.xml" -Raw
        $bm = [regex]::Matches($xmlContent, 'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
        if ($bm.Count -gt 0) {
            $last = $bm[$bm.Count - 1]
            $cx = ([int]$last.Groups[1].Value + [int]$last.Groups[3].Value) / 2
            $cy = ([int]$last.Groups[2].Value + [int]$last.Groups[4].Value) / 2
            & $Adb -s $deviceSerial shell input tap $cx $cy
        }
    }
    Start-Sleep -Seconds $PostLaunchWait

    # Step 9: wait, dismiss dialog, screenshot
    Start-Sleep -Seconds $ScreenshotWait
    Dismiss-SystemDialog | Out-Null
    & $Adb -s $deviceSerial shell screencap -p "/sdcard/screen_$package.png"
    & $Adb -s $deviceSerial pull "/sdcard/screen_$package.png" "$ScreenshotDir\screen_$package.png" | Out-Null
    Write-Host "   screenshot saved"

    # Step 10-11: back, force-stop HC, uninstall
    1..3 | ForEach-Object { & $Adb -s $deviceSerial shell input keyevent 4; Start-Sleep -Seconds 1 }
    & $Adb -s $deviceSerial shell am start -a android.settings.APPLICATION_DETAILS_SETTINGS -d "package:com.google.android.apps.healthdata" | Out-Null
    Start-Sleep -Seconds $PostLaunchWait
    & $Adb -s $deviceSerial shell am force-stop com.google.android.healthconnect.controller
    Start-Sleep -Seconds 3
    Uninstall-Package $package
    Write-Host "   done"
}

Write-Host "`n=== Batch complete: $current / $total ==="
