import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

globalThis.browser = undefined;
const require = createRequire(import.meta.url);
const { isSubmit, normalized, validateBinding } = require("../Extension/Resources/content.js");

test("requires exact URL, origin, and generation before mutation", () => {
  const url = "https://job-boards.greenhouse.io/embed/job_app?token=8168315&for=coinbase";
  const binding = { expectedUrl: url, expectedOrigin: "https://job-boards.greenhouse.io" };
  assert.equal(validateBinding(binding, `${url}#apply`, false), "");
  assert.equal(validateBinding({ ...binding, generation: "wrong" }, url, true), "document generation changed");
  assert.equal(validateBinding(binding, `${url}&other=1`, false), "document changed");
  assert.equal(validateBinding(binding, "https://example.com", false), "origin changed");
  assert.equal(normalized(`${url}#apply`), url);
});

test("submission controls are refused by classification", () => {
  const input = { matches: (selector) => selector.includes('input[type="submit"]'), tagName: "INPUT" };
  assert.equal(isSubmit(input), true);
  const button = { matches: () => false, tagName: "BUTTON", type: "" };
  assert.equal(isSubmit(button), true);
  const ordinary = { matches: () => false, tagName: "BUTTON", type: "button" };
  assert.equal(isSubmit(ordinary), false);
});
