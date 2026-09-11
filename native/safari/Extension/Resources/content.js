/* global browser */

(function () {
  "use strict";

  const PROTOCOL = 1;
  const MAX_ELEMENTS = 500;
  const TEXT_INPUTS = new Set(["", "text", "email", "tel", "url", "search", "number", "date", "month"]);
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
      SELECT: "combobox", H1: "heading", H2: "heading", H3: "heading"
    }[element.tagName] || element.tagName.toLowerCase());
  }

  function visible(element) {
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && !element.hidden;
  }

  function snapshot() {
    elements.clear();
    const selector = "a[href],button,input,textarea,select,[role=button],[role=checkbox],[role=combobox],[role=link],h1,h2,h3";
    const candidates = Array.from(document.querySelectorAll(selector)).filter(visible).slice(0, MAX_ELEMENTS);
    const lines = candidates.map((element) => {
      const attrs = [];
      const name = labelFor(element);
      if (name) attrs.push(`name=${JSON.stringify(name)}`);
      if (element.matches("input,textarea,select")) attrs.push(`filled=${Boolean(element.value)}`);
      if (element.required || element.getAttribute("aria-required") === "true") attrs.push("required=true");
      if (element.disabled) attrs.push("disabled=true");
      if (element.type === "checkbox" || element.getAttribute("role") === "checkbox") {
        attrs.push(`checked=${Boolean(element.checked || element.getAttribute("aria-checked") === "true")}`);
      }
      return `[${refFor(element)}] ${roleFor(element)}${attrs.length ? " " + attrs.join(" ") : ""}`;
    });
    if (candidates.length === MAX_ELEMENTS) lines.push("… snapshot truncated at 500 elements");
    return { tree: lines.join("\n"), elementCount: candidates.length };
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
    if (isSubmit(element)) return { status: "refused", reason: "form submission is outside the Safari MCP beta" };
    element.click();
    return { status: "performed_unverified" };
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
    if (!element?.isConnected || element.disabled || element.type !== "checkbox") {
      return { status: "refused", reason: "element is not an available native checkbox" };
    }
    if (element.checked !== Boolean(checked)) element.click();
    return element.checked === Boolean(checked) ? { status: "verified" } : { status: "failed", reason: "checkbox rejected the value" };
  }

  async function runAction(action) {
    const element = elements.get(action.ref);
    if (!element) return { status: "failed", reason: `stale or unknown ref: ${action.ref || "none"}` };
    if (action.action === "click") return click(element);
    if (action.action === "type" && element.getAttribute("role") === "combobox") {
      return chooseAriaOption(element, action.text ?? action.value ?? "");
    }
    if (action.action === "type") return type(element, action.text ?? action.value ?? "");
    if (action.action === "check") return check(element, action.checked);
    return { status: "refused", reason: `unsupported Safari action: ${action.action}` };
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
      const status = receipts.some((receipt) => receipt.status === "performed_unverified")
        ? "performed_unverified" : "verified";
      return { status, receipts, url: normalized(pageUrl), generation, ...after };
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
    module.exports = { handle, isSubmit, normalized, runAction, setNativeValue, validateBinding };
  }
}());
