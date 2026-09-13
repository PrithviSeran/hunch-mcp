---
name: document-editing
description: Edit and format existing documents in native or web document editors through Hunch, using precise selections and incremental in-document changes.
---

# Edit documents in place

Use this workflow when editing documents in Google Docs, Microsoft Word, Apple Pages, or other
native and web document editors. It is task-specific guidance, not an instruction to edit any
document merely because its app is open. Preserve the user's
document, tab, and editing mode. A request to revise or format a passage normally means editing
that passage in the existing document, not creating a replacement document or copying the whole
document into another chat. Whole-document rewriting is appropriate only when requested.

## Locate and select

Choose the control layer appropriate to the actual app. Prefer declared document-range APIs or
AppleScript operations when available; otherwise inspect native snapshot/act for native editors,
and web_snapshot/web_act for browser editors. Read the live menus, outline, toolbar, and dialogs.
Do not assume a Word, Pages, or other editor exposes Google's controls or shortcuts.

For browser editors, read web_snapshot for document tabs, outline headings, menus, toolbar controls, and dialogs.
Click the relevant tab or heading by its current ref to navigate. Canvas text may be absent from
the tree: use web_screenshot to see it and position the caret with web_act click_xy. On Safari,
coordinates include browser chrome and must come from a fresh screenshot before every click or
drag. Native act coordinates/keys are a different transport; use web_act for browser editing.
Safari's window-routed input is not a generic native-app input backend. For native apps, use
supported AX selection/actions or document APIs; if a needed operation requires gated foreground
input, respect that restriction. Do not replace an entire native text field just to change a range.

For a distant passage, use the editor's Find command and enter a distinctive
phrase from the requested passage. Inspect the match count and surrounding text; repeated phrases
need more context. Close Find, then inspect the actual document selection/caret. A highlighted
search match does not by itself prove the editor has a text selection. Do not type replacement
text until the selection or insertion point is confirmed.

Select the smallest exact range. For visible text, drag from its start to its end, then inspect
the highlighted range. For precise boundaries, position a caret and extend selection using
Shift+Left/Right. Common Mac shortcuts include Option+Shift+Left/Right for words and Command+Left/Right for a
line boundary; adding Shift extends selection to that boundary. Shift+Up/Down extends across
visual lines, which can wrap and are not necessarily paragraphs. A request for "two lines"
must be grounded in the visible text or named passage, not assumed to mean two paragraphs.
For offscreen endpoints, navigate and extend in small keyboard steps, inspecting as you go.
Do not blindly repeat a long sequence of keys or drag beyond the captured window.

## Make a local edit

In browser editors, use no-ref web_act type to insert at the caret or replace the selected text. Ref-based type
replaces a DOM field; an accessibility mirror of canvas text is not the document editor.
Change only the requested words or passage. Keep surrounding paragraphs, links, comments,
tables, and formatting intact. Use the document's existing menus/dialogs for structural edits.

For formatting, select the range first, then inspect the corresponding toolbar state. Use the
Italic button or Command+I for italics, Bold or Command+B for bold. These toggle formatting:
do not toggle an already-correct selection off, and inspect mixed selections before acting.
For "italicize only these two lines," verify the selection ends exactly at the requested range,
apply italics once, and check that adjacent text remains unchanged. Typing a passage again is
not a formatting operation.

Browser web_act Mac key example: `{"action":"key","key":"arrowright","modifiers":["shift"]}`.
Italic example: `{"action":"key","key":"i","modifiers":["command"]}`.
Use current toolbar/menu labels if shortcuts do not work; do not switch blindly between modifier
sets. Check the editor's current keyboard-shortcut help when the layout or platform differs.

## Verify and continue

Alternate observation, selection, one local change, and observation. After each meaningful edit,
check the resulting text or formatting, selection boundaries, and nearby unchanged content.
Inspect the save/sync indicator before reporting completion; use Save where the editor does not
autosave, without changing its existing file location or format. Tool dispatch is not proof of an
edit. On a wrong edit, inspect the latest change and use Undo only when it will undo that known
agent action; avoid undoing a collaborator's intervening work. Re-anchor when content shifts.

Do not use full-document copy/edit/paste as routine recovery from a thin tree or a selection
failure. Reinspect the canvas and try another local selection method. If the exact range remains
unverifiable after a bounded retry, report the specific unresolved selection instead of risking
a broad replacement. Respect runtime input restrictions; a capture refusal and an input refusal
are different capabilities. Never request disabling simultaneous mode simply because a web editor uses
a canvas. Do not write unsupported characters partially or silently drop them.

App-specific guidance: in Google Docs, distinguish document tabs from browser tabs and verify
Find's highlight becomes an actual editor selection. In Word or Pages, inspect the current
editing/review mode and range-selection facilities; preserve tracked changes, comments, and
document structure. In unfamiliar editors, discover equivalent controls from their tree and
menus before applying shortcuts. This workflow does not imply equal tool support in every app.

Google Docs shortcut reference: https://support.google.com/docs/answer/179738
