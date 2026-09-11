# Hunch Safari companion

This directory is the source of the signed macOS containing app and its Safari Web
Extension. Release CI builds and notarizes `Hunch Safari.app`, then sets
`HUNCH_SAFARI_COMPANION_APP` if the app is not at `native/dist/Hunch Safari.app`.
`hunch serve` copies that app into `/Applications` or `~/Applications` (Safari will not list an
extension buried in Application Support), launches it in the background, and prompts once to
enable it. There is no separate installer command.

The runtime contract is versioned in `src/hunch/safari.py`. The bridge accepts only typed
operations (`status`, `open`, `tabs`, `switch_tab`, `snapshot`, and `act`) and authenticates
every local request with the owner-only token created by the MCP process. Safari still owns
the two permissions Hunch cannot grant: enabling the extension and website access.

Production identifiers:

- Containing app: `com.tryhunch.safari`
- Safari Web Extension: `com.tryhunch.safari.Extension`
- Protocol: `1`

The bundle is assembled with SwiftPM and Command Line Tools, matching the existing Hunch macOS
release approach; full Xcode is not required. Developer-ID signing, notarization, and stapling
are still required before packaging a wheel. Running tests from a source checkout without the
app remains fully functional through the existing Chromium/CDP backend.

Build locally with `HUNCH_DEV_ID="Developer ID Application: …" scripts/build_safari_companion.sh`.
`scripts/release_safari_companion.sh` uses the same `HUNCH_NOTARY_PROFILE` convention as the
existing macOS app to submit and staple without full Xcode. Every macOS wheel then embeds
`native/dist/Hunch Safari.app` (override with `HUNCH_SAFARI_COMPANION_APP`). The build hook fails
if that artifact is absent.
