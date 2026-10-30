# Android 15 device fixture

This reuses the isolated transparent status-bar probe. It builds ordinary debug-signed APKs without network access, Gradle, Docker, installation, or device commands. No platform permission or shared UID is requested.

Build with JDK 17 and SDK platform 35 / build-tools 35.0.0:

```sh
JAVA_HOME=<jdk17> ANDROID_HOME=<sdk> node tests/device-fixture/build.mjs \
  "$HOME/dev/build/redroid-android15/artifacts/device-fixture"
```

All generated files, including the reusable private debug keystore, stay in that external output directory. `fixture-build.json` lists APK paths, package/component identities, source and APK hashes. The builder refuses an output inside this source repository.

Packages are `com.buildploy.statusbarprobe.session1` through `.session10`. They share one Java source and DEX, with distinct package/UID sandboxes. Odd sessions use the original light background and default dark icons; even sessions use the dark background and default light icons. Use session1/session2 for the dual-display stage, then add session3 through session10. The test model is Android user **0**, one independent application package per preview, not Android OS multi-user.

The following commands are instructions for the designated device owner; the builder does not run them. Supply the explicit serial and allocated display ID:

```sh
adb -s "$SERIAL" shell am start --user 0 --display "$DISPLAY" \
  -n com.buildploy.statusbarprobe.session1/probe.StatusBarProbe \
  --es requestId session1-write --es prefsWrite value-for-session1 \
  --ez edge true --ez darkIcons true --ez showStatus true --ez padInsets true
```

All original boolean options remain available: `edge`, `darkIcons`, `showStatus`, `padInsets`. The singleTop Activity accepts changes through `onNewIntent`. Omit `prefsWrite` to read without modifying storage; use a fresh `requestId` to correlate each query. Writes use a synchronous `SharedPreferences.commit()` to the fixed `user-value` key in the application's `MODE_PRIVATE` `device-fixture` preferences.

```sh
adb -s "$SERIAL" shell am start --user 0 --display "$DISPLAY" \
  -n com.buildploy.statusbarprobe.session1/probe.StatusBarProbe \
  --es requestId session1-read --ez darkIcons false
adb -s "$SERIAL" logcat -d -s BuildployStatusBarProbe:I '*:S'
```

Reports retain the `BUILDPLOY_STATUSBAR_PROBE` JSON prefix and all original actual `WindowInsets` fields, geometry, requested appearance and display ID. Added fields are `uid`, `requestId`, `privateValue` (JSON null before the first write), `preferenceWriteAttempted` and `preferenceWriteSucceeded`. A write is accepted only when the latter is true and the reported value matches. Reports appear after the Activity has real root WindowInsets; do not treat `am start` returning as a rendering assertion. Do not clear global logcat.

`topButtonBounds` contains actual screen coordinates `{left, top, right, bottom}` from the button's measured location and dimensions. Wait for positive width/height within the display, then use its center with `adb shell input -d "$DISPLAY" tap "$X" "$Y"`. Verify that `topButtonTaps` increases only in the target package/display report. Do not use fixed coordinates or infer a successful click from the input command's exit code.

For isolation, write distinct values to all ten packages and read each back with its actual UID/display identity. Force-stop and relaunch one package **without** `prefsWrite` to check persistence. Uninstall/reinstall one package without retaining data, then read it without `prefsWrite`: only that package should return null, while the other nine keep their values. Device operations belong to the acceptance driver, not this fixture build.

Android 15 target 35 enforces edge-to-edge. A requested FIT setting may be ignored; the report keeps requested settings separate from actual Insets and geometry. The top stripe is application content, not a simulated system status bar. SystemUI icon tint and transparency still require actual image/video validation.
