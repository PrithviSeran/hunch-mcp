#!/bin/bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
notary_profile="${HUNCH_NOTARY_PROFILE:-hunch-notary}"
dev_id="${HUNCH_DEV_ID:-}"

if [[ -z "$dev_id" ]]; then
  dev_id="$(security find-identity -v -p codesigning 2>/dev/null \
    | grep 'Developer ID Application' | head -1 | sed -E 's/.*"(.*)"/\1/')"
fi
if [[ -z "$dev_id" ]]; then
  echo "No Developer ID Application identity was found" >&2
  exit 1
fi
if ! xcrun notarytool history --keychain-profile "$notary_profile" >/dev/null 2>&1; then
  echo "No notary credentials found in keychain profile: $notary_profile" >&2
  exit 1
fi

export HUNCH_DEV_ID="$dev_id"
"$repo_root/scripts/build_safari_companion.sh"
app="$repo_root/native/dist/Hunch Safari.app"
submission="$repo_root/native/dist/Hunch-Safari-submit.zip"

rm -f "$submission"
ditto -c -k --keepParent "$app" "$submission"
xcrun notarytool submit "$submission" --keychain-profile "$notary_profile" --wait
rm -f "$submission"
xcrun stapler staple "$app"
xcrun stapler validate "$app"
spctl -a -vvv -t exec "$app"
# Ubuntu publish CI cannot notarize. Keep the stapled app in the Python package
# so `python -m build` on GitHub embeds the same binary users install from PyPI.
packaged="$repo_root/src/hunch/native/Hunch Safari.app"
rm -rf "$packaged"
mkdir -p "$(dirname "$packaged")"
ditto "$app" "$packaged"
echo "$packaged"
