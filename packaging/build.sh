#!/bin/bash
# Builds "Token Tracker.app" and "Token Tracker.dmg" into ./dist
set -euo pipefail
cd "$(dirname "$0")"
ROOT=..; DIST="$ROOT/dist"; APP="$DIST/Token Tracker.app"
rm -rf "$DIST"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# icon: SVG -> PNG (headless Chrome) -> .icns
if [ ! -f icon_1024.png ]; then
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu \
    --hide-scrollbars --default-background-color=00000000 --window-size=1024,1024 \
    --screenshot="$PWD/icon_1024.png" "file://$PWD/icon.svg" >/dev/null 2>&1
fi
ICONSET=$(mktemp -d)/AppIcon.iconset; mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s icon_1024.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) icon_1024.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

# native shell (universal: Apple Silicon + Intel)
swiftc -O -target arm64-apple-macos13 TokenTracker.swift -o /tmp/tt-arm64
swiftc -O -target x86_64-apple-macos13 TokenTracker.swift -o /tmp/tt-x86_64
lipo -create /tmp/tt-arm64 /tmp/tt-x86_64 -output "$APP/Contents/MacOS/Token Tracker"

cp "$ROOT/token_meter.py" "$ROOT/token_tui.py" "$ROOT/dashboard.html" "$APP/Contents/Resources/"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Token Tracker</string>
  <key>CFBundleDisplayName</key><string>Token Tracker</string>
  <key>CFBundleIdentifier</key><string>app.tokentracker.mac</string>
  <key>CFBundleExecutable</key><string>Token Tracker</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>Tracks Claude Code token usage locally.</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
codesign --force --deep -s - "$APP"

# drag-to-install disk image
STAGE=$(mktemp -d); cp -R "$APP" "$STAGE/"; ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Token Tracker" -srcfolder "$STAGE" -ov -format UDZO "$DIST/Token Tracker.dmg" >/dev/null
echo "Built: $APP"; echo "Built: $DIST/Token Tracker.dmg"
