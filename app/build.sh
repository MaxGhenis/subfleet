#!/bin/bash
# Build the Subfleet menu bar app: single-file SwiftUI -> /Applications/Subfleet.app
set -euo pipefail
cd "$(dirname "$0")"
APP="/Applications/Subfleet.app"

mkdir -p "$APP/Contents/MacOS"
swiftc -O -parse-as-library \
  -target arm64-apple-macos14.0 \
  SubfleetApp.swift \
  -o "$APP/Contents/MacOS/Subfleet"
cp Info.plist "$APP/Contents/Info.plist"
codesign --force -s - "$APP" >/dev/null 2>&1
echo "built: $APP"
