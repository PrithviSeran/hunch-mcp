#!/bin/bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
safari_root="$repo_root/native/safari"
output_root="${HUNCH_SAFARI_OUTPUT_DIR:-$repo_root/native/dist}"
app="$output_root/Hunch Safari.app"
extension="$app/Contents/PlugIns/Hunch.appex"
version="${HUNCH_SAFARI_VERSION:-0.1.0}"
build="${HUNCH_SAFARI_BUILD:-6}"

cd "$safari_root"
swift build -c release --product HunchSafariApp
swift build -c release --product HunchSafariExtension
bin_path="$(swift build -c release --show-bin-path)"

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources" \
  "$extension/Contents/MacOS" "$extension/Contents/Resources"
cp "$bin_path/HunchSafariApp" "$app/Contents/MacOS/Hunch Safari"
cp "$bin_path/HunchSafariExtension" "$extension/Contents/MacOS/Hunch"
cp App/Info.plist "$app/Contents/Info.plist"
cp App/Resources/hunch-build.json "$app/Contents/Resources/hunch-build.json"
cp Extension/Info.plist "$extension/Contents/Info.plist"
cp Extension/Resources/manifest.json Extension/Resources/background.js \
  Extension/Resources/content.js "$extension/Contents/Resources/"

logo="$repo_root/src/hunch/assets/hunch.png"
if [[ ! -f "$logo" ]]; then
  echo "Missing Hunch logo at $logo" >&2
  exit 1
fi
mkdir -p "$extension/Contents/Resources/images"
for size in 16 32 48 64 96 128 256 512; do
  sips -z "$size" "$size" "$logo" --out "$extension/Contents/Resources/images/icon-${size}.png" >/dev/null
done

iconset="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$iconset"
sips -z 16 16 "$logo" --out "$iconset/icon_16x16.png" >/dev/null
sips -z 32 32 "$logo" --out "$iconset/icon_16x16@2x.png" >/dev/null
sips -z 32 32 "$logo" --out "$iconset/icon_32x32.png" >/dev/null
sips -z 64 64 "$logo" --out "$iconset/icon_32x32@2x.png" >/dev/null
sips -z 128 128 "$logo" --out "$iconset/icon_128x128.png" >/dev/null
sips -z 256 256 "$logo" --out "$iconset/icon_128x128@2x.png" >/dev/null
sips -z 256 256 "$logo" --out "$iconset/icon_256x256.png" >/dev/null
sips -z 512 512 "$logo" --out "$iconset/icon_256x256@2x.png" >/dev/null
sips -z 512 512 "$logo" --out "$iconset/icon_512x512.png" >/dev/null
sips -z 1024 1024 "$logo" --out "$iconset/icon_512x512@2x.png" >/dev/null
iconutil -c icns "$iconset" -o "$app/Contents/Resources/AppIcon.icns"
rm -rf "$(dirname "$iconset")"


set_plist() { /usr/libexec/PlistBuddy -c "Set :$2 $3" "$1"; }
set_plist "$app/Contents/Info.plist" CFBundleExecutable "Hunch Safari"
set_plist "$app/Contents/Info.plist" CFBundleIdentifier com.tryhunch.safari
set_plist "$app/Contents/Info.plist" CFBundleShortVersionString "$version"
set_plist "$app/Contents/Info.plist" CFBundleVersion "$build"
set_plist "$app/Contents/Info.plist" LSMinimumSystemVersion 13.0
set_plist "$extension/Contents/Info.plist" CFBundleExecutable Hunch
set_plist "$extension/Contents/Info.plist" CFBundleIdentifier com.tryhunch.safari.Extension
set_plist "$extension/Contents/Info.plist" CFBundleShortVersionString "$version"
set_plist "$extension/Contents/Info.plist" CFBundleVersion "$build"
set_plist "$extension/Contents/Info.plist" \
  NSExtension:NSExtensionPrincipalClass SafariWebExtensionHandler
cp "$app/Contents/Resources/AppIcon.icns" "$extension/Contents/Resources/AppIcon.icns"
/usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string AppIcon" "$app/Contents/Info.plist" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :CFBundleIconFile AppIcon" "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string AppIcon" "$extension/Contents/Info.plist" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :CFBundleIconFile AppIcon" "$extension/Contents/Info.plist"

sign_id="${HUNCH_DEV_ID:-}"
if [[ -z "$sign_id" ]]; then
  sign_id="$(security find-identity -v -p codesigning 2>/dev/null \
    | grep 'Developer ID Application' | head -1 | sed -E 's/.*"(.*)"/\1/')"
fi
sign_id="${sign_id:--}"
sign_args=(--force --options runtime --sign "$sign_id")
if [[ "$sign_id" != "-" ]]; then sign_args+=(--timestamp); fi
codesign "${sign_args[@]}" --entitlements Extension/HunchSafariExtension.entitlements "$extension"
codesign "${sign_args[@]}" --entitlements App/HunchSafari.entitlements "$app"
codesign --verify --deep --strict --verbose=2 "$app"
echo "$app"
