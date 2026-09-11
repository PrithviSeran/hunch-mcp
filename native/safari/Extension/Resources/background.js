/* global browser */

(function () {
  "use strict";

  const PROTOCOL = 1;
  const APP_ID = "com.tryhunch.safari"; // Safari ignores this value.

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
        title: tab.title || "",
        url: tab.url,
        current: Boolean(tab.active)
      }));
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
      return { protocol: PROTOCOL, id: request.id, tabId: tab.id, ...result };
    } catch (error) {
      return reply(request, "blocked", {
        reason: `Hunch does not have website access for this tab: ${String(error)}`
      });
    }
  }

  async function waitForContent(request, command, allowUnbound) {
    let response;
    for (let attempt = 0; attempt < 20; attempt += 1) {
      response = await sendToBoundTab(request, command, allowUnbound);
      if (response.status !== "blocked" || !String(response.reason).includes("website access")) {
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
      if (!payload.url) {
        return reply(request, "blocked", { reason: "Safari web_open requires an exact URL" });
      }
      let tab;
      try {
        const matches = (await browser.tabs.query({})).filter(
          (candidate) => candidate.url && normalized(candidate.url) === normalized(payload.url)
        );
        if (matches.length > 1) {
          return reply(request, "blocked", {
            reason: `expected one Safari tab at the requested URL, found ${matches.length}`
          });
        }
        tab = matches[0] || await browser.tabs.create({ url: payload.url, active: false });
      } catch (error) {
        return reply(request, "failed", { reason: String(error) });
      }
      return waitForContent(
        { ...request, payload: { tabId: tab.id } }, { name: "snapshot" }, true
      );
    }
    if (request.operation === "switch_tab") {
      const tabs = await allTabs();
      const selected = tabs[payload.index];
      if (!selected) return reply(request, "blocked", { reason: "Safari tab index is out of range" });
      return sendToBoundTab(
        { ...request, payload: { tabId: selected.tabId } }, { name: "snapshot" }, true
      );
    }
    if (request.operation === "snapshot") {
      return sendToBoundTab(request, { name: "snapshot" });
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
        await browser.tabs.update(tab.id, { url: navigation.url, active: false });
        return waitForContent(
          { ...request, payload: { tabId: tab.id } }, { name: "snapshot" }, true
        );
      }
      return sendToBoundTab(request, { name: "act", actions: payload.actions || [] });
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
    module.exports = { handle, normalized, origin, unwrap };
  }
}());
