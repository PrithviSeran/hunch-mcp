import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

let tabs = [
  { id: 7, title: "Job", url: "https://job-boards.greenhouse.io/embed/job_app?token=8168315&for=coinbase", active: false }
];
const posted = [];
globalThis.browser = {
  tabs: {
    query: async () => tabs,
    create: async ({ url, active }) => ({ id: 9, url, active }),
    sendMessage: async (id) => ({ status: "verified", url: tabs.find((tab) => tab.id === id).url,
      generation: "doc-1", tree: "[e1] textbox", elementCount: 1 })
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
const { handle, unwrap } = require("../Extension/Resources/background.js");

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

test("refuses an ambiguous exact URL", async () => {
  tabs = [...tabs, { ...tabs[0], id: 8 }];
  const request = { protocol: 1, id: "request-2", operation: "open", payload: { url: tabs[0].url } };
  const result = await handle(request);
  assert.equal(result.status, "blocked");
  assert.match(result.reason, /found 2/);
});
