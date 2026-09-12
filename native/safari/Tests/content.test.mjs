import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

globalThis.browser = undefined;
const require = createRequire(import.meta.url);
const { click, isSubmit, normalized, validateBinding } = require("../Extension/Resources/content.js");

test("requires exact URL, origin, and generation before mutation", () => {
  const url = "https://job-boards.greenhouse.io/embed/job_app?token=8168315&for=coinbase";
  const binding = { expectedUrl: url, expectedOrigin: "https://job-boards.greenhouse.io" };
  assert.equal(validateBinding(binding, `${url}#apply`, false), "");
  assert.equal(validateBinding({ ...binding, generation: "wrong" }, url, true), "document generation changed");
  assert.equal(validateBinding(binding, `${url}&other=1`, false), "document changed");
  assert.equal(validateBinding(binding, "https://example.com", false), "origin changed");
  assert.equal(normalized(`${url}#apply`), url);
});

test("submission controls are identified and clicked", () => {
  const input = { matches: (selector) => selector.includes('input[type="submit"]'), tagName: "INPUT" };
  assert.equal(isSubmit(input), true);
  let clicks = 0;
  const button = { matches: () => false, tagName: "BUTTON", type: "", isConnected: true,
    disabled: false, click: () => { clicks += 1; } };
  assert.equal(isSubmit(button), true);
  assert.deepEqual(click(button), { status: "performed_unverified", effect: "submit" });
  assert.equal(clicks, 1);
  const ordinary = { matches: () => false, tagName: "BUTTON", type: "button" };
  assert.equal(isSubmit(ordinary), false);
});
