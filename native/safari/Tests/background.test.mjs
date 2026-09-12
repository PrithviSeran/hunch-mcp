import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

let tabs = [
  { id: 7, windowId: 2, title: "Job", url: "https://job-boards.greenhouse.io/embed/job_app?token=8168315&for=coinbase", active: false }
];
let windows = [{ id: 1, focused: true }, { id: 2, focused: false }];
const posted = [];
globalThis.browser = {
  tabs: {
    query: async (query = {}) => tabs.filter((tab) => query.windowId === undefined || tab.windowId === query.windowId),
    create: async ({ url, active }) => ({ id: 9, windowId: 2, url, active }),
    update: async (id, changes) => {
      const tab = tabs.find((candidate) => candidate.id === id);
      Object.assign(tab, changes);
      if (changes.active) {
        for (const other of tabs.filter((candidate) => candidate.windowId === tab.windowId && candidate.id !== id)) {
          other.active = false;
        }
      }
      return tab;
    },
    captureVisibleTab: async () => "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
    sendMessage: async (id) => ({ status: "verified", url: tabs.find((tab) => tab.id === id).url,
      generation: "doc-1", tree: "[e1] textbox", elementCount: 1,
      viewportWidth: 100, viewportHeight: 50, devicePixelRatio: 2 })
  },
  windows: {
    getAll: async () => windows,
    create: async ({ url, focused }) => {
      const created = { id: 3, focused, tabs: [{ id: 9, windowId: 3, url, active: true }] };
      windows.push({ id: 3, focused });
      tabs.push(created.tabs[0]);
      return created;
    }
  },
  runtime: {
    connectNative: () => ({
      onMessage: { addListener: () => {} },
      onDisconnect: { addListener: () => {} },
      postMessage: (value) => posted.push(value)
    })
  }
};

const require = createRequire(import.meta.url);
const { handle, mapVisualActions, unwrap } = require("../Extension/Resources/background.js");

test("unwraps Safari native-message envelopes", () => {
  const inner = { protocol: 1, id: "request-9", operation: "status" };
  assert.deepEqual(unwrap({ name: "hunch-command", userInfo: inner }), {
    name: "hunch-command", ...inner
  });
  assert.deepEqual(unwrap(inner), inner);
});

test("reuses exactly one existing Safari tab without activating it", async () => {
  const request = { protocol: 1, id: "request-1", operation: "open", payload: { url: tabs[0].url } };
  const result = await handle(request);
  assert.equal(result.status, "verified");
  assert.equal(result.tabId, 7);
  assert.equal(result.generation, "doc-1");
});

test("creates and owns an unfocused Safari window", async () => {
  const request = { protocol: 1, id: "request-window", operation: "open",
    payload: { url: "https://example.com/drive", newWindow: true } };
  const result = await handle(request);
  assert.equal(result.status, "verified");
  assert.equal(result.windowId, 3);
  assert.equal(result.tabId, 9);
  assert.equal(result.ownedWindow, true);
  assert.ok(result.windowLease);
  assert.equal(windows.find((item) => item.id === 1).focused, true);

  const shot = await handle({ protocol: 1, id: "request-shot", operation: "act",
    payload: { tabId: result.tabId, windowId: result.windowId, windowLease: result.windowLease,
      expectedUrl: result.url, expectedOrigin: "https://example.com", generation: result.generation,
      captureScreenshot: true } });
  assert.equal(shot.status, "verified");
  assert.equal(shot.mimeType, "image/png");
  assert.equal(shot.pixelWidth, 1);
  assert.equal(shot.pixelHeight, 1);

  tabs.push({ id: 10, windowId: result.windowId, title: "Second", url: "https://example.com/second", active: false });
  const selected = await handle({ protocol: 1, id: "request-switch", operation: "switch_tab",
    payload: { index: tabs.findIndex((tab) => tab.id === 10), windowId: result.windowId,
      windowLease: result.windowLease } });
  assert.equal(selected.status, "verified");
  assert.equal(selected.tabId, 10);
  assert.equal(tabs.find((tab) => tab.id === 10).active, true);
  assert.equal(windows.find((item) => item.id === 1).focused, true);
});

test("maps screenshot pixels into CSS viewport coordinates", () => {
  const [click] = mapVisualActions([{ action: "click_xy", x: 100, y: 50 }], {
    pixelWidth: 200, pixelHeight: 100, viewportWidth: 100, viewportHeight: 50
  });
  assert.deepEqual(click, { action: "click_xy", x: 50, y: 25 });
});

test("refuses an ambiguous exact URL", async () => {
  tabs = [...tabs, { ...tabs[0], id: 8 }];
  const request = { protocol: 1, id: "request-2", operation: "open", payload: { url: tabs[0].url } };
  const result = await handle(request);
  assert.equal(result.status, "blocked");
  assert.match(result.reason, /found 2/);
});
