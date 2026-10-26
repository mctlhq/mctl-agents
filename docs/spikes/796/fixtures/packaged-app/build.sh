#!/bin/sh
# JVM jar fallback (an APK needs the Android SDK/Gradle toolchain; the report
# must state which was used). Prints the jar sha256. Timestamps are fixed.
set -eu
cd "$(dirname "$0")"
mkdir -p build/classes
javac --release 17 -d build/classes src/demo/Flags.java
jar --create --file build/app.jar --main-class demo.Flags --date 2026-01-01T00:00:00Z -C build/classes .
sha256sum build/app.jar | cut -d' ' -f1
