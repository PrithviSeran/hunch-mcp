---
name: google-docs-editing
description: Edit and format existing Google Docs through Hunch's browser controls, with precise text selection and incremental in-document changes.
---

# Edit Google Docs in place

Use this workflow for existing documents at docs.google.com/document/. Preserve the user's
document, tab, and editing mode. A request to revise or format a passage normally means editing
that passage in the existing document, not creating a replacement document or copying the whole
document into another chat. Whole-document rewriting is appropriate only when requested.

## Locate and select

Read web_snapshot for document tabs, outline headings, menus, toolbar controls, and dialogs.
Click the relevant tab or heading by its current ref to navigate. Canvas text may be absent from
the tree: use web_screenshot to see it and position the caret with web_act click_xy. On Safari,
coordinates include browser chrome and must come from a fresh screenshot before every click or
drag. Native act coordinates/keys are a different transport; use web_act for browser editing.

For a distant passage, use the document's Find command (Command+F on Mac) and enter a distinctive
phrase from the requested passage. Inspect the match count and surrounding text; repeated phrases
need more context. Close Find, then inspect the actual document selection/caret. A highlighted
search match does not by itself prove the editor has a text selection. Do not type replacement
text until the selection or insertion point is confirmed.

Select the smallest exact range. For visible text, drag from its start to its end, then inspect
the highlighted range. For precise boundaries, position a caret and extend selection using
Shift+Left/Right; Option+Shift+Left/Right extends by words on Mac. Command+Left/Right moves to a
line boundary; adding Shift extends selection to that boundary. Shift+Up/Down extends across
visual lines, which can wrap and are not necessarily paragraphs. A request for "two lines"
must be grounded in the visible text or named passage, not assumed to mean two paragraphs.
For offscreen endpoints, navigate and extend in small keyboard steps, inspecting as you go.
Do not blindly repeat a long sequence of keys or drag beyond the captured window.

## Make a local edit

Use no-ref web_act type to insert at the caret or replace the selected text. Ref-based type
replaces a DOM field; an accessibility mirror of canvas text is not the document editor.
Change only the requested words or passage. Keep surrounding paragraphs, links, comments,
tables, and formatting intact. Use the document's existing menus/dialogs for structural edits.

For formatting, select the range first, then inspect the corresponding toolbar state. Use the
Italic button or Command+I for italics, Bold or Command+B for bold. These toggle formatting:
do not toggle an already-correct selection off, and inspect mixed selections before acting.
For "italicize only these two lines," verify the selection ends exactly at the requested range,
apply italics once, and check that adjacent text remains unchanged. Typing a passage again is
not a formatting operation.

Mac Hunch key example: `{"action":"key","key":"arrowright","modifiers":["shift"]}`.
Italic example: `{"action":"key","key":"i","modifiers":["command"]}`.
Use current toolbar/menu labels if shortcuts do not work; do not switch blindly between modifier
sets. Check the editor's current keyboard-shortcut help when the layout or platform differs.

## Verify and continue

Alternate observation, selection, one local change, and observation. After each meaningful edit,
check the resulting text or formatting, selection boundaries, and nearby unchanged content.
Inspect the save/sync indicator before reporting completion. Tool dispatch is not proof of an
edit. On a wrong edit, inspect the latest change and use Undo only when it will undo that known
agent action; avoid undoing a collaborator's intervening work. Re-anchor when content shifts.

Do not use full-document copy/edit/paste as routine recovery from a thin tree or a selection
failure. Reinspect the canvas and try another local selection method. If the exact range remains
unverifiable after a bounded retry, report the specific unresolved selection instead of risking
a broad replacement. Respect runtime input restrictions; a capture refusal and an input refusal
are different capabilities. Never request disabling simultaneous mode simply because Docs uses
a canvas. Do not write unsupported characters partially or silently drop them.

Shortcut reference: https://support.google.com/docs/answer/179738
