/* global browser */

(function () {
  "use strict";

  const PROTOCOL = 1;
  const APP_ID = "com.tryhunch.safari"; // Safari ignores this value.
  const ownedWindows = new Map();
  const lastCaptures = new Map();

  function reply(request, status, detail = {}) {
    return { protocol: PROTOCOL, id: request?.id, status, ...detail };
  }

  function unwrap(message) {
    if (!message || typeof message !== "object") return message;
    if (message.userInfo && typeof message.userInfo === "object") {
      return { name: message.name, ...message.userInfo };
    }
    return message;
  }

  function normalized(value) {
    const url = new URL(value);
    url.hash = "";
    return url.href;
  }

  function origin(value) {
    return new URL(value).origin;
  }

  async function allTabs() {
    const tabs = await browser.tabs.query({});
    return tabs
      .filter((tab) => /^https?:/.test(tab.url || ""))
      .map((tab, index) => ({
        index,
        tabId: tab.id,
        windowId: tab.windowId,
        title: tab.title || "",
        url: tab.url,
        current: Boolean(tab.active)
      }));
  }

  async function focusedWindowId() {
    if (!browser.windows?.getAll) return null;
    const windows = await browser.windows.getAll();
    return windows.find((candidate) => candidate.focused)?.id ?? null;
  }

  function ownership(tab, payload) {
    if (!payload.windowId || tab.windowId !== payload.windowId) {
      return { windowId: tab.windowId, ownedWindow: false };
    }
    const lease = ownedWindows.get(tab.windowId);
    return {
      windowId: tab.windowId,
      ownedWindow: Boolean(lease && payload.windowLease === lease),
      ...(lease && payload.windowLease === lease ? { windowLease: lease } : {})
    };
  }

  function decorate(request, tab, result) {
    return { protocol: PROTOCOL, id: request.id, tabId: tab.id, ...ownership(tab, request.payload || {}), ...result };
  }

  async function sendToBoundTab(request, command, allowUnbound = false) {
    const payload = request.payload || {};
    const tabs = await browser.tabs.query({});
    const tab = tabs.find((candidate) => candidate.id === payload.tabId);
    if (!tab) return reply(request, "blocked", { reason: "bound Safari tab no longer exists" });
    if (!allowUnbound) {
      if (!payload.expectedUrl || normalized(tab.url) !== normalized(payload.expectedUrl)) {
        return reply(request, "blocked", { reason: "bound Safari document navigated" });
      }
      if (!payload.expectedOrigin || origin(tab.url) !== payload.expectedOrigin) {
        return reply(request, "blocked", { reason: "bound Safari origin changed" });
      }
    }
    try {
      if (browser.permissions?.contains) {
        try {
          const allowed = await browser.permissions.contains({ origins: [`${origin(tab.url)}/*`] });
          if (!allowed) return reply(request, "blocked", {
            code: "WEBSITE_ACCESS_DENIED", reason: "Hunch does not have website access for this tab"
          });
        } catch (_error) {
          // Safari versions differ in permission introspection; sendMessage remains authoritative.
        }
      }
      const result = await browser.tabs.sendMessage(tab.id, {
        protocol: PROTOCOL,
        id: request.id,
        binding: {
          expectedUrl: allowUnbound ? tab.url : payload.expectedUrl,
          expectedOrigin: allowUnbound ? origin(tab.url) : payload.expectedOrigin,
          generation: payload.generation || null
        },
        command
      });
      return decorate(request, tab, result);
    } catch (error) {
      return reply(request, "blocked", {
        code: "CONTENT_SCRIPT_NOT_READY",
        reason: `Hunch content script is not ready for this tab: ${String(error)}`
      });
    }
  }

  function pngSize(dataUrl) {
    try {
      const bytes = atob(String(dataUrl).split(",", 2)[1] || "");
      if (bytes.slice(1, 4) !== "PNG") return {};
      const value = (offset) => (((bytes.charCodeAt(offset) << 24) >>> 0)
        + (bytes.charCodeAt(offset + 1) << 16) + (bytes.charCodeAt(offset + 2) << 8)
        + bytes.charCodeAt(offset + 3));
      return { pixelWidth: value(16), pixelHeight: value(20) };
    } catch (_error) {
      return {};
    }
  }

  function mapVisualActions(actions, capture) {
    if (!actions.some((action) => ["click_xy", "drag"].includes(action.action))) return actions;
    if (!capture?.pixelWidth || !capture?.pixelHeight || !capture?.viewportWidth || !capture?.viewportHeight) {
      return null;
    }
    const xScale = capture.viewportWidth / capture.pixelWidth;
    const yScale = capture.viewportHeight / capture.pixelHeight;
    return actions.map((action) => {
      if (action.action === "click_xy") return { ...action, x: action.x * xScale, y: action.y * yScale };
      if (action.action === "drag") return {
        ...action, from_x: action.from_x * xScale, from_y: action.from_y * yScale,
        to_x: action.to_x * xScale, to_y: action.to_y * yScale
      };
      return action;
    });
  }

  async function settleAfterAct(request, beforeTabs, firstResult) {
    const canSettle = ["verified", "performed_unverified"].includes(firstResult.status)
      || firstResult.code === "CONTENT_SCRIPT_NOT_READY";
    if (!canSettle) return firstResult;
    const payload = request.payload || {};
    for (let attempt = 0; attempt < 12; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 100));
      const tabs = await browser.tabs.query({});
      const created = tabs.filter((tab) => !beforeTabs.some((old) => old.id === tab.id)
        && (!payload.windowId || tab.windowId === payload.windowId));
      if (created.length === 1) {
        return waitForContent(
          { ...request, payload: { ...payload, tabId: created[0].id, expectedUrl: created[0].url } },
          { name: "snapshot" }, true
        );
      }
      const tab = tabs.find((candidate) => candidate.id === payload.tabId);
      if (!tab) return reply(request, "blocked", { code: "BOUND_TAB_CLOSED", reason: "bound Safari tab no longer exists" });
      if (normalized(tab.url) !== normalized(payload.expectedUrl)) {
        return waitForContent(
          { ...request, payload: { ...payload, expectedUrl: tab.url } }, { name: "snapshot" }, true
        );
      }
    }
    return firstResult;
  }

  async function waitForContent(request, command, allowUnbound) {
    let response;
    for (let attempt = 0; attempt < 20; attempt += 1) {
      response = await sendToBoundTab(request, command, allowUnbound);
      if (response.status !== "blocked" || response.code !== "CONTENT_SCRIPT_NOT_READY") {
        return response;
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    return response;
  }

  async function handle(request) {
    if (!request || request.protocol !== PROTOCOL || !request.id || !request.operation) {
      return reply(request, "refused", { reason: "invalid bridge request" });
    }
    const payload = request.payload || {};

    if (request.operation === "status") {
      return reply(request, "verified", { extensionEnabled: true, tabs: await allTabs() });
    }
    if (request.operation === "tabs") {
      return reply(request, "verified", { tabs: await allTabs() });
    }
    if (request.operation === "open") {
      let tab;
      try {
        if (payload.newWindow) {
          if (!payload.url) {
            return reply(request, "blocked", { reason: "opening a new Safari window requires an exact URL" });
          }
          if (!browser.windows?.create) {
            return reply(request, "refused", { code: "ACTION_UNSUPPORTED", reason: "this Safari version cannot create extension windows" });
          }
          const focusedBefore = await focusedWindowId();
          const created = await browser.windows.create({ url: payload.url, focused: false, type: "normal" });
          tab = created.tabs?.[0] || (await browser.tabs.query({ windowId: created.id }))[0];
          if (!tab) return reply(request, "failed", { reason: "Safari created a window without a tab" });
          const focusedAfter = await focusedWindowId();
          if (focusedBefore !== null && focusedAfter !== null && focusedBefore !== focusedAfter) {
            try { await browser.windows.remove?.(created.id); } catch (_error) { /* best effort */ }
            return reply(request, "failed", { code: "FOCUS_CHANGED", reason: "Safari focused the new window instead of opening it in the background" });
          }
          const lease = crypto.randomUUID();
          ownedWindows.set(created.id, lease);
          const response = await waitForContent(
            { ...request, payload: { tabId: tab.id, windowId: created.id, windowLease: lease } },
            { name: "snapshot" }, true
          );
          return { ...response, windowId: created.id, windowLease: lease, ownedWindow: true };
        } else if (payload.url) {
          const matches = (await browser.tabs.query({})).filter(
            (candidate) => candidate.url && normalized(candidate.url) === normalized(payload.url)
          );
          if (matches.length > 1) {
            return reply(request, "blocked", {
              reason: `expected one Safari tab at the requested URL, found ${matches.length}`
            });
          }
          tab = matches[0] || await browser.tabs.create({ url: payload.url, active: false });
        } else {
          const tabs = await browser.tabs.query({});
          const focused = await focusedWindowId();
          const selected = tabs.filter((candidate) => candidate.active
            && (focused === null || candidate.windowId === focused));
          if (selected.length !== 1) {
            return reply(request, "blocked", {
              code: "SELECTED_TAB_AMBIGUOUS",
              reason: "could not identify one selected Safari tab; pass its exact URL"
            });
          }
          [tab] = selected;
        }
      } catch (error) {
        return reply(request, "failed", { reason: String(error) });
      }
      return waitForContent(
        { ...request, payload: { tabId: tab.id, windowId: tab.windowId } }, { name: "snapshot" }, true
      );
    }
    if (request.operation === "switch_tab") {
      const tabs = await allTabs();
      const selected = tabs[payload.index];
      if (!selected) return reply(request, "blocked", { reason: "Safari tab index is out of range" });
      const lease = ownedWindows.get(selected.windowId);
      if (lease && payload.windowId === selected.windowId && payload.windowLease === lease) {
        const focusedBefore = await focusedWindowId();
        await browser.tabs.update(selected.tabId, { active: true });
        const focusedAfter = await focusedWindowId();
        if (focusedBefore !== null && focusedAfter !== null && focusedBefore !== focusedAfter) {
          return reply(request, "failed", {
            code: "FOCUS_CHANGED",
            reason: "Safari focused the Hunch window while selecting its background tab"
          });
        }
      }
      return sendToBoundTab(
        { ...request, payload: { ...payload, tabId: selected.tabId, windowId: selected.windowId } },
        { name: "snapshot" }, true
      );
    }
    if (request.operation === "snapshot") {
      return sendToBoundTab(request, { name: "snapshot" });
    }
    if (request.operation === "act" && payload.captureScreenshot) {
      const tabs = await browser.tabs.query({});
      const tab = tabs.find((candidate) => candidate.id === payload.tabId);
      if (!tab) return reply(request, "blocked", { code: "BOUND_TAB_CLOSED", reason: "bound Safari tab no longer exists" });
      if (!tab.active) {
        return reply(request, "blocked", {
          code: "BOUND_TAB_INACTIVE",
          reason: "Safari can screenshot only the selected tab in a window; select it or bind the currently selected tab"
        });
      }
      try {
        const view = await sendToBoundTab(request, { name: "snapshot" });
        if (view.status !== "verified") return view;
        const focusedBefore = await focusedWindowId();
        const dataUrl = await browser.tabs.captureVisibleTab(tab.windowId, { format: "png" });
        const focusedAfter = await focusedWindowId();
        if (focusedBefore !== null && focusedAfter !== null && focusedBefore !== focusedAfter) {
          return reply(request, "failed", {
            code: "FOCUS_CHANGED",
            reason: "Safari focused the Hunch window while capturing its background tab"
          });
        }
        const capture = {
          ...pngSize(dataUrl), viewportWidth: view.viewportWidth, viewportHeight: view.viewportHeight,
          devicePixelRatio: view.devicePixelRatio || 1
        };
        lastCaptures.set(tab.id, capture);
        return decorate(request, tab, {
          status: "verified", url: tab.url, generation: view.generation,
          mimeType: "image/png", data: String(dataUrl).split(",", 2)[1] || "", ...capture
        });
      } catch (error) {
        return reply(request, "failed", { code: "SCREENSHOT_FAILED", reason: String(error) });
      }
    }
    if (request.operation === "act") {
      const actions = payload.actions || [];
      const navigation = actions.find((action) => action.action === "navigate");
      if (navigation) {
        if (actions.length !== 1 || !navigation.url) {
          return reply(request, "refused", { reason: "navigation must be one bound action" });
        }
        const tabs = await browser.tabs.query({});
        const tab = tabs.find((candidate) => candidate.id === payload.tabId);
        if (!tab || normalized(tab.url) !== normalized(payload.expectedUrl)) {
          return reply(request, "blocked", { reason: "bound Safari document navigated" });
        }
        lastCaptures.delete(tab.id);
        await browser.tabs.update(tab.id, { url: navigation.url });
        return waitForContent(
          { ...request, payload: { ...payload, tabId: tab.id, expectedUrl: navigation.url } },
          { name: "snapshot" }, true
        );
      }
      const mapped = mapVisualActions(actions, lastCaptures.get(payload.tabId));
      if (!mapped) {
        return reply(request, "refused", {
          code: "SCREENSHOT_REQUIRED", reason: "take a fresh Safari web_screenshot before coordinate actions"
        });
      }
      lastCaptures.delete(payload.tabId);
      const beforeTabs = await browser.tabs.query({});
      const result = await sendToBoundTab(request, {
        name: "act", actions: mapped, postcondition: payload.postcondition || null
      });
      return settleAfterAct(request, beforeTabs, result);
    }
    return reply(request, "refused", { reason: "unsupported bridge operation" });
  }

  async function deliverReceipt(request, result) {
    const receipt = {
      ...result,
      kind: "extension_receipt",
      receiptPort: request?.receiptPort,
      receiptNonce: request?.receiptNonce
    };
    if (nativePort) {
      nativePort.postMessage(receipt);
      return;
    }
    if (typeof browser.runtime.sendNativeMessage === "function") {
      await browser.runtime.sendNativeMessage(APP_ID, receipt);
    }
  }

  async function process(raw) {
    const request = unwrap(raw);
    if (!request || request.kind === "idle" || request.queued || request.accepted) return;
    if (!request.operation) return;
    await deliverReceipt(request, await handle(request));
  }

  let nativePort;
  function connect() {
    nativePort = browser.runtime.connectNative(APP_ID);
    nativePort.onMessage.addListener((request) => { process(request); });
    nativePort.onDisconnect.addListener(() => {
      nativePort = undefined;
      setTimeout(connect, 500);
    });
  }

  async function pull() {
    if (typeof browser.runtime.sendNativeMessage !== "function") return;
    try {
      await process(await browser.runtime.sendNativeMessage(APP_ID, {
        kind: "pull", protocol: PROTOCOL
      }));
    } catch (_error) {
      // Native messaging is unavailable until Safari finishes loading the extension.
    }
  }

  if (typeof browser !== "undefined" && browser.runtime?.connectNative) {
    connect();
  }
  if (typeof browser !== "undefined" && typeof browser.runtime?.sendNativeMessage === "function") {
    pull();
    setInterval(pull, 400);
  }
  if (typeof browser !== "undefined" && browser.runtime?.onMessage?.addListener) {
    browser.runtime.onMessage.addListener((message) => {
      if (message?.type === "wake") pull();
    });
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { handle, mapVisualActions, normalized, origin, pngSize, unwrap };
  }
}());
