/* global browser */

(function () {
  "use strict";

  const PROTOCOL = 1;
  const MAX_ELEMENTS = 500;
  const TEXT_INPUTS = new Set([
    "", "text", "email", "tel", "url", "search", "number", "date", "month", "week",
    "time", "datetime-local"
  ]);
  const SEMANTIC_SELECTOR = [
    "a[href]", "button", "input", "textarea", "select", "[contenteditable=true]",
    "[role=button]", "[role=checkbox]", "[role=combobox]", "[role=gridcell]",
    "[role=link]", "[role=listbox]", "[role=menuitem]", "[role=option]",
    "[role=radio]", "[role=searchbox]", "[role=slider]", "[role=switch]",
    "[role=tab]", "[role=textbox]", "[role=treeitem]", "h1", "h2", "h3"
  ].join(",");
  const refs = new WeakMap();
  const elements = new Map();
  const generation = crypto.randomUUID();
  let nextRef = 1;

  function normalized(value) {
    const url = new URL(value);
    url.hash = "";
    return url.href;
  }

  function validateBinding(binding, pageUrl = window.location.href, mutating = false) {
    if (!binding?.expectedUrl || !binding?.expectedOrigin) return "missing page binding";
    let expected;
    let actual;
    try {
      expected = new URL(binding.expectedUrl);
      actual = new URL(pageUrl);
    } catch (_error) {
      return "invalid page binding";
    }
    if (expected.origin !== binding.expectedOrigin || actual.origin !== binding.expectedOrigin) {
      return "origin changed";
    }
    if (normalized(expected.href) !== normalized(actual.href)) return "document changed";
    if (mutating && binding.generation !== generation) return "document generation changed";
    return "";
  }

  function refFor(element) {
    let ref = refs.get(element);
    if (!ref) {
      ref = `e${nextRef++}`;
      refs.set(element, ref);
    }
    elements.set(ref, element);
    return ref;
  }

  function clean(value, limit = 160) {
    return String(value || "").replace(/\s+/g, " ").trim().slice(0, limit);
  }

  function labelFor(element) {
    const labelledBy = element.getAttribute("aria-labelledby");
    if (labelledBy) {
      const value = labelledBy.split(/\s+/).map((id) => document.getElementById(id)?.textContent).join(" ");
      if (clean(value)) return clean(value);
    }
    const explicit = element.getAttribute("aria-label");
    if (explicit) return clean(explicit);
    if (element.id) {
      const label = document.querySelector(`label[for="${CSS.escape(element.id)}"]`);
      if (label) return clean(label.textContent);
    }
    return clean(element.innerText || element.textContent || element.getAttribute("placeholder"));
  }

  function roleFor(element) {
    return element.getAttribute("role") || ({
      A: "link", BUTTON: "button", INPUT: "textbox", TEXTAREA: "textbox",
      SELECT: "combobox", H1: "heading", H2: "heading", H3: "heading",
      CANVAS: "canvas"
    }[element.tagName] || element.tagName.toLowerCase());
  }

  function visible(element) {
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && !element.hidden;
  }

  function semanticElements(root = document) {
    const found = [];
    function visit(scope) {
      for (const element of Array.from(scope.querySelectorAll(SEMANTIC_SELECTOR))) {
        found.push(element);
        if (element.shadowRoot) visit(element.shadowRoot);
      }
    }
    visit(root);
    return found;
  }

  function boundsFor(element) {
    const rect = element.getBoundingClientRect?.();
    if (!rect || (!rect.width && !rect.height)) return "";
    return `${Math.round(rect.x)},${Math.round(rect.y)},${Math.round(rect.width)},${Math.round(rect.height)}`;
  }

  function snapshot() {
    elements.clear();
    const all = semanticElements().filter(visible);
    const candidates = all.slice(0, MAX_ELEMENTS);
    const lines = candidates.map((element) => {
      const attrs = [];
      const name = labelFor(element);
      if (name) attrs.push(`name=${JSON.stringify(name)}`);
      if (element.matches("input,textarea,select,[contenteditable=true]")) {
        attrs.push(`filled=${Boolean(element.value || clean(element.textContent))}`);
      }
      if (element.tagName === "A" && element.href) attrs.push(`href=${JSON.stringify(element.href)}`);
      if (element.required || element.getAttribute("aria-required") === "true") attrs.push("required=true");
      if (element.disabled || element.getAttribute("aria-disabled") === "true") attrs.push("disabled=true");
      for (const state of ["expanded", "pressed", "selected"]) {
        const value = element.getAttribute(`aria-${state}`);
        if (value === "true" || value === "false") attrs.push(`${state}=${value}`);
      }
      if (element.type === "checkbox" || ["checkbox", "switch"].includes(element.getAttribute("role"))) {
        attrs.push(`checked=${Boolean(element.checked || element.getAttribute("aria-checked") === "true")}`);
      }
      const bounds = boundsFor(element);
      if (bounds) attrs.push(`bounds=${bounds}`);
      return `[${refFor(element)}] ${roleFor(element)}${attrs.length ? " " + attrs.join(" ") : ""}`;
    });
    if (all.length > MAX_ELEMENTS) lines.push(`… snapshot truncated at ${MAX_ELEMENTS} of ${all.length} elements`);
    return {
      tree: lines.join("\n"), elementCount: candidates.length,
      viewportWidth: window.innerWidth, viewportHeight: window.innerHeight,
      devicePixelRatio: window.devicePixelRatio || 1
    };
  }

  function editEvents(element) {
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function setNativeValue(element, value) {
    const prototype = element.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
    if (!setter) return false;
    setter.call(element, String(value));
    editEvents(element);
    return element.value === String(value);
  }

  function isSubmit(element) {
    if (!element) return false;
    if (element.matches('input[type="submit"],input[type="image"]')) return true;
    return element.tagName === "BUTTON" && (!element.type || element.type.toLowerCase() === "submit");
  }

  function click(element) {
    if (!element?.isConnected || element.disabled) return { status: "failed", reason: "element is unavailable" };
    try { element.focus?.({ preventScroll: true }); } catch (_error) { element.focus?.(); }
    element.click();
    return { status: "performed_unverified", effect: isSubmit(element) ? "submit" : "click" };
  }

  function type(element, value) {
    if (!element?.isConnected || element.disabled || element.readOnly) {
      return { status: "failed", reason: "field is unavailable" };
    }
    const tag = element.tagName;
    const inputType = (element.type || "").toLowerCase();
    if (inputType === "password" || inputType === "file") {
      return { status: "refused", reason: "secret and file inputs require a dedicated gated operation" };
    }
    if (tag === "SELECT") {
      const option = Array.from(element.options).find((item) => clean(item.textContent) === clean(value));
      if (!option) return { status: "failed", reason: `option not found: ${clean(value)}` };
      element.value = option.value;
      editEvents(element);
      return element.value === option.value ? { status: "verified" } : { status: "failed", reason: "selection was rejected" };
    }
    if (element.isContentEditable) {
      element.focus();
      const selection = window.getSelection?.();
      const range = document.createRange?.();
      if (selection && range) {
        range.selectNodeContents(element);
        selection.removeAllRanges();
        selection.addRange(range);
      }
      let inserted = false;
      try { inserted = Boolean(document.execCommand?.("insertText", false, String(value))); } catch (_error) { /* fallback below */ }
      if (!inserted) {
        element.textContent = String(value);
        element.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: String(value) }));
      }
      element.dispatchEvent(new Event("change", { bubbles: true }));
      return clean(element.textContent, String(value).length + 1) === clean(value, String(value).length + 1)
        ? { status: "verified" } : { status: "performed_unverified" };
    }
    if (tag !== "TEXTAREA" && (tag !== "INPUT" || !TEXT_INPUTS.has(inputType))) {
      return { status: "refused", reason: "element is not a supported text field" };
    }
    return setNativeValue(element, value) ? { status: "verified" } : { status: "failed", reason: "page rejected the value" };
  }

  function waitFor(predicate, timeoutMs = 1500) {
    const started = Date.now();
    return new Promise((resolve) => {
      function checkNow() {
        const result = predicate();
        if (result || Date.now() - started >= timeoutMs) return resolve(result || null);
        setTimeout(checkNow, 25);
      }
      checkNow();
    });
  }

  async function chooseAriaOption(element, value) {
    if (!element?.isConnected || element.disabled || element.getAttribute("role") !== "combobox") {
      return { status: "refused", reason: "element is not an available ARIA combobox" };
    }
    element.click();
    const wanted = clean(value);
    const option = await waitFor(() => {
      // Scope to this combobox's own listbox. A page may contain several hidden option lists;
      // a global [role=option] query selected the wrong Greenhouse control in the POC.
      const listboxId = element.getAttribute("aria-controls");
      const listbox = listboxId ? document.getElementById(listboxId) : null;
      return Array.from(listbox?.querySelectorAll('[role="option"]') || [])
        .find((candidate) => clean(candidate.textContent) === wanted);
    });
    if (!option) return { status: "failed", reason: `option not found: ${wanted}` };
    option.click();
    const selected = await waitFor(() => {
      const listboxId = element.getAttribute("aria-controls");
      const selectedId = element.getAttribute("aria-activedescendant");
      const selectedOption = selectedId ? document.getElementById(selectedId) : null;
      const control = element.closest('[class*="control"]') || element.parentElement?.parentElement;
      return clean(selectedOption?.textContent) === wanted || clean(control?.textContent).includes(wanted);
    });
    return selected ? { status: "verified" } : { status: "failed", reason: "selection was not reflected in the control" };
  }

  function check(element, checked) {
    const role = element?.getAttribute?.("role");
    if (!element?.isConnected || element.disabled || (element.type !== "checkbox" && !["checkbox", "switch"].includes(role))) {
      return { status: "refused", reason: "element is not an available checkbox or switch" };
    }
    const current = Boolean(element.checked || element.getAttribute("aria-checked") === "true");
    if (current !== Boolean(checked)) element.click();
    const after = Boolean(element.checked || element.getAttribute("aria-checked") === "true");
    return after === Boolean(checked) ? { status: "verified" } : { status: "failed", reason: "checkbox rejected the value" };
  }

  function deepElementFromPoint(x, y, root = document) {
    let element = root.elementFromPoint?.(x, y) || null;
    while (element?.shadowRoot) {
      const nested = element.shadowRoot.elementFromPoint?.(x, y);
      if (!nested || nested === element) break;
      element = nested;
    }
    return element;
  }

  function actionable(element) {
    return element?.closest?.(SEMANTIC_SELECTOR) || element;
  }

  function pointerEvent(name, x, y, buttons) {
    const EventClass = typeof PointerEvent === "function" ? PointerEvent : MouseEvent;
    return new EventClass(name, { bubbles: true, cancelable: true, clientX: x, clientY: y, buttons });
  }

  function clickAt(x, y) {
    const raw = deepElementFromPoint(x, y);
    const element = actionable(raw);
    if (!element) return { status: "failed", reason: "no element at screenshot coordinate" };
    if (element.tagName !== "CANVAS") return click(element);
    element.focus?.({ preventScroll: true });
    for (const [name, buttons] of [["pointerdown", 1], ["mousedown", 1], ["pointerup", 0], ["mouseup", 0], ["click", 0]]) {
      element.dispatchEvent(pointerEvent(name, x, y, buttons));
    }
    return { status: "performed_unverified", effect: "synthetic_canvas_click" };
  }

  function dragAt(fromX, fromY, toX, toY) {
    const element = deepElementFromPoint(fromX, fromY);
    if (!element) return { status: "failed", reason: "no element at drag start coordinate" };
    for (const [name, x, y, buttons] of [
      ["pointerdown", fromX, fromY, 1], ["mousedown", fromX, fromY, 1],
      ["pointermove", toX, toY, 1], ["mousemove", toX, toY, 1],
      ["pointerup", toX, toY, 0], ["mouseup", toX, toY, 0]
    ]) element.dispatchEvent(pointerEvent(name, x, y, buttons));
    return { status: "performed_unverified", effect: "synthetic_drag" };
  }

  function key(element, value) {
    const name = String(value || "").toLowerCase();
    if (["return", "enter"].includes(name) && element?.form) {
      element.form.requestSubmit(isSubmit(element) ? element : undefined);
      return { status: "performed_unverified", effect: "submit" };
    }
    if (name === "pagedown" || name === "pageup") {
      const before = window.scrollY;
      window.scrollBy({ top: (name === "pagedown" ? 1 : -1) * Math.max(1, window.innerHeight * 0.8), behavior: "instant" });
      return window.scrollY !== before ? { status: "verified", effect: "scroll" }
        : { status: "performed_unverified", effect: "scroll" };
    }
    if (!element) return { status: "refused", reason: "Safari key action needs a field ref except for page scrolling" };
    element.focus?.();
    for (const type of ["keydown", "keyup"]) {
      element.dispatchEvent(new KeyboardEvent(type, { key: value, bubbles: true, cancelable: true }));
    }
    return { status: "performed_unverified", effect: "synthetic_key" };
  }

  async function runAction(action) {
    if (action.action === "click_xy") return clickAt(Number(action.x), Number(action.y));
    if (action.action === "drag") {
      return dragAt(Number(action.from_x), Number(action.from_y), Number(action.to_x), Number(action.to_y));
    }
    const element = elements.get(action.ref) || (!action.ref ? document.activeElement : null);
    if (action.action === "key") return key(element || document.activeElement, action.key);
    if (!element) return { status: "failed", reason: `stale or unknown ref: ${action.ref || "none"}` };
    if (action.action === "click") return click(element);
    if (action.action === "type" && element.getAttribute("role") === "combobox") {
      return chooseAriaOption(element, action.text ?? action.value ?? "");
    }
    if (action.action === "type") return type(element, action.text ?? action.value ?? "");
    if (action.action === "check") return check(element, action.checked);
    return { status: "refused", reason: `unsupported Safari action: ${action.action}` };
  }

  function evaluatePostcondition(spec) {
    if (!spec) return null;
    const element = elements.get(spec.ref);
    if (!element) return { matched: false, error: "postcondition element unavailable", ...spec };
    const readers = {
      value: () => element.value ?? element.textContent,
      title: () => element.getAttribute("title"),
      enabled: () => !(element.disabled || element.getAttribute("aria-disabled") === "true"),
      selected: () => element.selected ?? element.checked ?? element.getAttribute("aria-selected"),
      expanded: () => element.getAttribute("aria-expanded")
    };
    if (!readers[spec.field]) return { matched: false, error: "postcondition field unavailable", ...spec };
    const actual = readers[spec.field]();
    return { ...spec, actual, matched: actual === spec.equals };
  }

  async function handle(request, pageUrl = window.location.href) {
    const mutating = request?.command?.name === "act";
    const error = validateBinding(request?.binding, pageUrl, mutating);
    if (error) return { status: "blocked", reason: error, url: normalized(pageUrl), generation };
    if (request.protocol !== PROTOCOL) return { status: "refused", reason: "protocol mismatch" };
    if (request.command.name === "snapshot") {
      return { status: "verified", url: normalized(pageUrl), generation, ...snapshot() };
    }
    if (request.command.name === "act") {
      const receipts = [];
      for (const action of request.command.actions || []) {
        const receipt = await runAction(action);
        receipts.push(receipt);
        if (!["verified", "performed_unverified"].includes(receipt.status)) {
          return { status: receipt.status, reason: receipt.reason, receipts, url: normalized(pageUrl), generation };
        }
      }
      const after = snapshot();
      const postcondition = evaluatePostcondition(request.command.postcondition);
      const status = receipts.some((receipt) => receipt.status === "performed_unverified")
        ? "performed_unverified" : "verified";
      return { status, receipts, url: normalized(pageUrl), generation, ...after,
        ...(postcondition ? { postcondition } : {}) };
    }
    return { status: "refused", reason: "unsupported content command" };
  }

  if (typeof browser !== "undefined" && browser.runtime?.onMessage) {
    browser.runtime.onMessage.addListener((request) => handle(request));
  }
  if (typeof browser !== "undefined" && browser.runtime?.sendMessage) {
    try { browser.runtime.sendMessage({ type: "wake" }); } catch (_error) { /* ignore */ }
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      actionable, click, clickAt, deepElementFromPoint, evaluatePostcondition, handle, isSubmit,
      normalized, runAction,
      setNativeValue, validateBinding
    };
  }
}());
