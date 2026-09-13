# Background Safari input validation — 2026-09-12

Status: **candidate, not release-ready**. No live SDK installation or release performed.

## Verified live

- Disposable localhost canvas backed by a hidden textarea: trusted native click and
  input events, selection/replacement, accented text and BMP Unicode; foreground app
  and pointer unchanged in the short isolated test.
- Real private scratch Google Doc, through `SafariComputer.act`: multiline insertion,
  Cmd+F search, Escape, replacement of Q21 without changing Q13, and document rename.
- Google Docs reported Saved to Drive. An independently downloaded text export proved
  the original multiline insertion persisted, despite blank canvas screenshots.
- ScreenCaptureKit subsequently captured the actual current canvas, visibly confirming
  the Q21 replacement and unchanged Q13. The final clean test also visibly passed.
- Every reported foreground-app comparison during the Google Docs actions was unchanged.
- Window-owner routing passed 30 consecutive read-only resolutions after replacing
  an intermittent legacy GetProcessForPID lookup failure.
- 307 automated tests pass. These are not a substitute for the live reliability gate.

## Remaining release blockers / constraints

1. Repeated ScreenCaptureKit capture eventually returned
   `com.apple.ScreenCaptureKit.SCStreamErrorDomain -3811`, “Failed to start stream due
   to audio/video capture failure.” The window remained visible and not minimized.
   Read-only retries and an experimental macOS 26 still-image selector did not recover
   it. The still-image experiment was removed. Exact root cause is not established;
   do not call this a permission denial or claim capture reliability has passed.
2. Supplementary Unicode (the tested emoji) was dropped by the native key-event path
   in Google Docs. Candidate now refuses such text before typing, rather than silently
   losing characters. Full supplementary-Unicode input remains unfinished.
3. Legacy screencapture and Safari captureVisibleTab omitted current canvas/composited
   content in this test. Do not silently fall back to either as proof of a current image.
4. Requires Accessibility/Screen Recording, a unique matching URL selected in its Safari
   window, and another foreground app. Duplicate URLs, changed targets, reloaded page
   generations, moved/resized windows and changed foreground apps fail closed.
5. Human typing concurrently with a long run has not been instrumented. The implementation
   posts only to Safari's process/window, never the global HID/session input stream or
   clipboard. Do not overstate what the foreground/pointer checks alone prove.

## Reproduce

Run `PYTHONPATH=src python tests/manual_safari_input.py --bridge` on a permissioned Mac.
For a separately authorized scratch document use `tests/manual_google_docs.py URL` and
its explicit `--actions` option. Screenshots are full native-window pixels, including
browser chrome; do not reuse page-only coordinates. Never run these probes against a
valuable document. Capture failures may leave a previous PNG on disk: the driver error
means that image is stale and must not be interpreted as a fresh capture.
