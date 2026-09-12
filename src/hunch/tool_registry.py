"""Canonical public schemas and catalog membership for every Hunch transport."""
_ACTION_ITEM = {
    "type": "object",
    "properties": {
        "action": {"type": "string",
                   "enum": ["click", "right_click", "select", "type", "menu", "key", "window", "drag", "click_xy"]},
        "ref": {"type": "string"}, "text": {"type": "string"},
        "path": {"type": "array", "items": {"type": "string"}},
        "key": {"type": "string"}, "modifiers": {"type": "array", "items": {"type": "string"}},
        "x": {"type": "integer"}, "y": {"type": "integer"}, "w": {"type": "integer"}, "h": {"type": "integer"},
        "app": {"type": "string"},
        "from_ref": {"type": "string"}, "to_ref": {"type": "string"},
        "from_x": {"type": "integer"}, "from_y": {"type": "integer"},
        "to_x": {"type": "integer"}, "to_y": {"type": "integer"}},
    "required": ["action"]}

# The web/CDP action item. Its coordinates are renderer-local and focus-free, unlike native pixels.
_WEB_ACTION_ITEM = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["click", "click_xy", "drag", "type", "key", "navigate"]},
        "ref": {"type": "string"}, "text": {"type": "string"},
        "key": {"type": "string"}, "modifiers": {"type": "array", "items": {"type": "string"}},
        "url": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
        "from_x": {"type": "number"}, "from_y": {"type": "number"},
        "to_x": {"type": "number"}, "to_y": {"type": "number"}},
    "required": ["action"]}


def _obj(props=None, required=None):
    return {"type": "object", "properties": props or {}, "additionalProperties": False,
            **({"required": required} if required else {})}


# Names identical to the MCP tools so every HUNCH_PLAYBOOK reference resolves. Descriptions
# are condensed (the deep procedural guidance lives in the playbook system prompt); each keeps
# the prescriptive when-to-call sentence, which measurably lifts should-call rate on Opus.
BASE_TOOLS = [
    {"name": "snapshot",
     "description": ("See an app as an accessibility tree ([ref] per element) — your primary way "
                     "to read UI focus-free. Pass an app name or leave blank for the frontmost. "
                     "ref='e42' re-walks just that element's subtree; truncated trees end in `…` "
                     "markers naming what was dropped."),
     "input_schema": _obj({"app": {"type": "string"}, "ref": {"type": "string"},
                           "max_depth": {"type": "integer"}, "max_nodes": {"type": "integer"},
                           "max_children": {"type": "integer"}})},
    {"name": "find",
     "description": ("Search an app's WHOLE tree for matching elements without reading a full "
                     "snapshot — the cheap way to locate one control in a big window. Filter by "
                     "role (e.g. 'button', case-insensitive) and/or name_contains (substring of "
                     "title/description/value). Returns actable [ref]s."),
     "input_schema": _obj({"role": {"type": "string"}, "name_contains": {"type": "string"},
                           "app": {"type": "string"}, "max_results": {"type": "integer"}})},
    {"name": "act",
     "description": ("Run UI actions in order by ref, then get what CHANGED on screen (unchanged lines omitted; snapshot gives the full tree). To tile/position a window use the 'window' verb (x/y/w/h, focus-free, targets the main window; pass `app` to target a specific app — to tile TWO apps give each window action its own `app`, e.g. app:'TextEdit' x:0 then app:'Notes' x:756). Verbs: click "
                     "(activate), right_click (context menu), select (highlight a row), type (set "
                     "a field by ref = focus-free; no ref types at focus = STEALS FOCUS), menu "
                     "(invoke a menu-bar path, e.g. ['File','Move to Trash'] — the focus-free "
                     "stand-in for ⌘-shortcuts), key/click_xy (STEAL FOCUS, last resort). Prefer "
                     "the focus-free verbs. Pass `reason` when any action steals focus."),
     "input_schema": _obj({"actions": {"type": "array", "items": _ACTION_ITEM},
                           "reason": {"type": "string"}}, ["actions"])},
    {"name": "screenshot",
     "description": ("See the frontmost app as a PNG — only for genuinely visual content the tree "
                     "can't convey (an image, a chart). To read UI text or check an action, "
                     "re-snapshot instead. Image pixels are in point space, so a coordinate read "
                     "here can go straight to a click_xy action."),
     "input_schema": _obj()},
    {"name": "list_apps", "description": "List the running GUI apps you can target with snapshot.",
     "input_schema": _obj()},
    {"name": "launch_app",
     "description": ("Launch or focus an app (reliable OS call — beats clicking the Dock). Set "
                     "force_accessibility=true for an Electron/Chromium app whose tree reads empty. "
                     "In simultaneous mode it launches in the background."),
     "input_schema": _obj({"name": {"type": "string"},
                           "force_accessibility": {"type": "boolean"},
                           "reason": {"type": "string"}}, ["name"])},
    {"name": "simultaneous_mode",
     "description": ("Toggle simultaneous mode. ON = never steal the user's cursor/keyboard/view "
                     "(background reads, focus-free actions only, shared-input actions refused). "
                     "OFF = may bring apps forward and use the full input set."),
     "input_schema": _obj({"on": {"type": "boolean"}})},
    {"name": "quit_app", "description": "Quit an app via the OS (reliable regardless of focus).",
     "input_schema": _obj({"name": {"type": "string"}}, ["name"])},
    {"name": "focus_app",
     "description": ("Bring an app to the front and target it. This switches the user's view — "
                     "always pass `reason` (one short sentence for why)."),
     "input_schema": _obj({"name": {"type": "string"}, "reason": {"type": "string"}}, ["name"])},
    {"name": "web_open",
     "description": ("Open Safari through the Hunch extension, or a Chromium/Electron app over "
                     "CDP, for FOCUS-FREE background control. `app` is the browser (default "
                     "'Google Chrome'; use 'Safari' for the user's existing Safari session); "
                     "put a website in `url`. Chromium uses the persistent Hunch "
                     "profile; call web_login once if it isn't signed in. For Safari, new_window=true "
                     "creates the dedicated unfocused window required by web_screenshot/click_xy. CODE EDITORS: app="
                     "'Cursor'/'Visual Studio Code'/'VSCodium'/'Windsurf' with the FOLDER/FILE in "
                     "`url` opens a dedicated background editor window whose integrated TERMINAL you "
                     "can type into (AX can't write it) — snapshot, then web_act 'type' on the "
                     "'Terminal' tab (trailing newline runs the command); key ctrl+` opens one."),
     "input_schema": _obj({"app": {"type": "string"}, "url": {"type": "string"},
                           "isolated": {"type": "boolean"}, "new_window": {"type": "boolean"}})},
    {"name": "web_login",
     "description": ("Open a background, banner-tagged window for the HUMAN to sign in once (Hunch "
                     "never sees the password); the login then persists. Uses the configured "
                     "user-attention notification when enabled."),
     "input_schema": _obj({"app": {"type": "string"}, "url": {"type": "string"}})},
    {"name": "web_snapshot",
     "description": "Read the bound Safari or CDP page as an accessibility tree. Call web_open first.",
     "input_schema": _obj()},
    {"name": "web_screenshot",
     "description": ("PNG of the bound CDP page or Hunch-owned Safari background window "
                     "(focus-free) — for visual web content the tree can't convey. Use this, never "
                     "the OS screenshot, for the background browser."),
     "input_schema": _obj()},
    {"name": "web_act",
     "description": ("Run focus-free page actions, then get the updated tree. Verbs: click (by ref), "
                     "click_xy/drag (coordinates from web_screenshot; canvas editors), type "
                     "(with ref REPLACES a field; without ref types at current focus), key, navigate "
                     "(only to a URL you were given or read from the page — click links, don't guess)."),
     "input_schema": _obj({"actions": {"type": "array", "items": _WEB_ACTION_ITEM}}, ["actions"])},
    {"name": "web_restart",
     "description": ("Recover a BROKEN CDP browser by quitting and reopening it fresh (login kept). "
                     "Last resort — don't restart a merely-slow page; wait and re-snapshot first."),
     "input_schema": _obj({"app": {"type": "string"}, "url": {"type": "string"}})},
    {"name": "web_tabs",
     "description": "List open tabs in the bound Safari or CDP session, including Safari window identity.",
     "input_schema": _obj()},
    {"name": "web_switch_tab",
     "description": "Switch the bound Safari or CDP session to a tab by index, then web_snapshot.",
     "input_schema": _obj({"index": {"type": "integer"}}, ["index"])},
    {"name": "list_credentials",
     "description": ("List the service NAMES the user saved credentials for (names + kind only, "
                     "never values). Fill them with web_fill_login / web_fill_secret."),
     "input_schema": _obj()},
    {"name": "web_fill_login",
     "description": ("Fill the current CDP page's login form from the user's saved credential for "
                     "`service`, WITHOUT the values entering your context — you only learn which "
                     "fields were filled. Then submit via web_act. web_open first."),
     "input_schema": _obj({"service": {"type": "string"}}, ["service"])},
    {"name": "web_fill_secret",
     "description": ("Type the user's saved protected value (API key/token) for `service` into a "
                     "field on the current CDP page, WITHOUT the value entering your context. "
                     "web_snapshot first and pass the field's ref."),
     "input_schema": _obj({"service": {"type": "string"}, "ref": {"type": "string"}}, ["service"])},
    {"name": "notify_user",
     "description": ("Alert the user when you need them SHORTLY — to finish a login, approve a 2FA "
                     "prompt, solve a captcha, or make a decision only they can. Call this the moment "
                     "you hit a step only the human can do."),
     "input_schema": _obj({"message": {"type": "string"}}, ["message"])},
    {"name": "request_focus",
     "description": ("Ask the user's permission BEFORE a focus-stealing step you'll do via other "
                     "tools. Pops a one-click Go ahead / Cancel dialog and returns their choice."),
     "input_schema": _obj({"reason": {"type": "string"}}, ["reason"])},
    {"name": "trash",
     "description": ("Move file(s)/folder(s) to the Trash by path — focus-free and reversible. Use "
                     "this to delete files instead of driving Finder."),
     "input_schema": _obj({"paths": {"type": "array", "items": {"type": "string"}}}, ["paths"])},
    {"name": "file_op",
     "description": ("Focus-free filesystem ops by path: op='move'|'copy' (src -> dst) or op='mkdir' "
                     "(create a folder at src). For MULTIPLE operations pass batch=[{op,src,dst},...] "
                     "in ONE call (e.g. sort a whole folder at once). To delete, use trash."),
     "input_schema": _obj({"op": {"type": "string", "enum": ["move", "copy", "mkdir"]},
                           "src": {"type": "string"}, "dst": {"type": "string"},
                           "batch": {"type": "array", "items": {
                               "type": "object", "properties": {
                                   "op": {"type": "string", "enum": ["move", "copy", "mkdir"]},
                                   "src": {"type": "string"}, "dst": {"type": "string"}},
                               "required": ["op", "src"]}}})},
    {"name": "open_file",
     "description": ("Open a file/folder/URL/app-deep-link with its default (or a named) app — "
                     "focus-free launch (also 'mailto:', 'spotify:track:...')."),
     "input_schema": _obj({"path": {"type": "string"}, "app": {"type": "string"}}, ["path"])},
    {"name": "reveal_in_finder",
     "description": "Reveal/select item(s) in a Finder window by path (this does front Finder).",
     "input_schema": _obj({"paths": {"type": "array", "items": {"type": "string"}}}, ["paths"])},
    {"name": "clipboard_get", "description": "Read the clipboard's text — focus-free (no ⌘C).",
     "input_schema": _obj()},
    {"name": "clipboard_set", "description": "Put text on the clipboard — focus-free (no ⌘V).",
     "input_schema": _obj({"text": {"type": "string"}}, ["text"])},
    {"name": "applescript",
     "description": ("Run AppleScript to control scriptable native apps FOCUS-FREE via Apple Events "
                     "— Mail, Messages, Notes, Reminders, Calendar, Music, Finder, Safari. Prefer "
                     "this over UI-driving those apps. Risky scripts prompt the user."),
     "input_schema": _obj({"script": {"type": "string"}}, ["script"])},
]

for _tool in BASE_TOOLS:
    if _tool["name"] in ("act", "web_act"):
        _tool["description"] += " Set detailed=true for a structured receipt; optional postcondition verifies a final field value, not every intermediate action."
        _tool["input_schema"]["properties"].update({
            "detailed": {"type": "boolean"},
            "postcondition": _obj({"ref": {"type": "string"},
                                   "field": {"type": "string", "enum": ["value", "title", "enabled", "selected", "expanded"]},
                                   "equals": {"type": ["string", "number", "boolean", "null"]}}, ["ref", "field", "equals"])})

RECOVERY_TOOLS = [
    {"name": "app_target", "description": "Inspect exact running processes and native windows; optionally select without activation. Use inventory='installed' for a separate bounded bundle inventory.",
     "input_schema": _obj({"app": {"type": "string"}, "window": {"type": "string"}, "select": {"type": "boolean"},
                           "inventory": {"type": "string", "enum": ["running", "installed"]}})},
    {"name": "app_capabilities", "description": "Inspect operation-specific AX/CDP evidence and bounded recovery options.",
     "input_schema": _obj({"app": {"type": "string"}, "operation": {"type": "string"}}, ["app"])},
    {"name": "app_recover", "description": "Execute a fresh target-bound recovery plan under current host policy.",
     "input_schema": _obj({"plan_id": {"type": "string"}, "target_id": {"type": "string"}}, ["plan_id"])},
]


def catalog(mode="runtime"):
    if mode != "runtime":
        raise ValueError("unknown tool catalog")
    return [{**tool, "input_schema": {**tool["input_schema"], "additionalProperties": False}}
            for tool in BASE_TOOLS + RECOVERY_TOOLS]


def catalog_for(mac):
    return catalog()


def validate_arguments(name, arguments):
    """Apply the same model-call contract after every transport's decoding."""
    from jsonschema import Draft202012Validator
    from .errors import HunchError
    definition = next(tool for tool in catalog() if tool["name"] == name)
    schema = {**definition["input_schema"], "additionalProperties": False}
    if isinstance(arguments, dict):
        # MCP wrappers supply None for omitted optional parameters. Preserve explicit
        # nullable values and required fields; normalize only omission sentinels.
        arguments = {key: value for key, value in arguments.items()
                     if value is not None or key in schema.get("required", [])
                     or "null" in schema.get("properties", {}).get(key, {}).get("type", [])}
    error = next(Draft202012Validator(schema).iter_errors(arguments), None)
    if error:
        # Do not echo input values (which may contain protected app data).
        path = ".".join(str(part) for part in error.absolute_path) or "arguments"
        raise HunchError(f"invalid {name} {path}: violates {error.validator}; use the published tool schema")
    return arguments
