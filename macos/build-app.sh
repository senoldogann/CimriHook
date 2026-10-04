#!/bin/sh
# Builds "CimriHook Bar.app" into macos/build: a menu bar only app, signed ad hoc for this Mac.
set -eu
cd "$(dirname "$0")"
swift build -c release
app="build/CimriHook Bar.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS"
cp "$(swift build -c release --show-bin-path)/CimriHookBar" "$app/Contents/MacOS/CimriHookBar"
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>CimriHookBar</string>
    <key>CFBundleIdentifier</key><string>io.github.cimrihook.bar</string>
    <key>CFBundleName</key><string>CimriHook Bar</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST
codesign --force --sign - "$app"
echo "$app"
