// The extension's first tests. Run:  node --test apps/extension/test_fill.mjs
// No dependencies and no toolchain: this repo has no package.json, and a test that needs an npm
// install is a test nobody runs. The verifier touches five DOM calls, so those five are stubbed and
// the REAL code is loaded out of content.js — if the shipped logic changes, these fail.
//
// What they pin is the one distinction the fill flow turns on: "we typed a value" and "the form
// accepted it" are different facts. Measured against the widget shapes an ATS actually ships, only
// one reads back correctly while the form holds nothing — a combobox whose committed value lives in
// a hidden input — and that is the field a submit button calls empty while the user can see it filled.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const SRC = readFileSync(new URL("./content.js", import.meta.url), "utf8");
const slice = (a, b) => SRC.slice(SRC.indexOf(a), SRC.indexOf(b));

// ── the smallest DOM these functions actually use ────────────────────────────────────────────────
const el2 = (o = {}) => ({
  value: o.value ?? "", id: o.id ?? "", checked: !!o.checked,
  getAttribute(n) { return (o.attrs || {})[n] ?? null; },
});
const boxOf = (hidden) => ({ querySelectorAll: () => hidden });

function verifier(docHidden = [], opts = {}) {
  globalThis.CSS = { escape: (s) => s };
  globalThis.document = {
    querySelectorAll: () => docHidden,
    querySelector: () => (opts.listOpen ? { textContent: "an option" } : null),
    // a real page always has a body; the stub has one so "can I tell?" and "is it gone?" stay
    // different questions
    body: { contains: () => opts.detached !== true },
  };
  const src = `
    const norm = s => (s || "").toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\\s+/g, " ").trim();
    ${slice('  const OK = "verified"', "  function verifyChecked")}
    ${slice("  function verifyChecked", "  function findAll")}
    return { OK, SOFT, NONE, verifyValue, verifyChecked, committedTwin };`;
  return new Function(src)();
}

test("a field that reads back what we set is verified", () => {
  const v = verifier();
  assert.equal(v.verifyValue(el2({ value: "Sandeep Gupta" }), "Sandeep Gupta", null), v.OK);
});

test("an empty field is missing, not merely unconfirmed", () => {
  const v = verifier();
  assert.equal(v.verifyValue(el2({ value: "" }), "Sandeep Gupta", null), v.NONE);
});

test("THE BUG: a combobox whose hidden committed value is empty is NOT verified, however right it looks", () => {
  const v = verifier();
  const visible = el2({ value: "San Francisco, CA", attrs: { name: "loc_search" } });
  const box = boxOf([el2({ value: "" })]);           // the twin the site validates — still empty
  assert.equal(v.verifyValue(visible, "San Francisco, CA", box), v.SOFT);
});

test("the same combobox IS verified once the hidden twin carries the value", () => {
  const v = verifier();
  const visible = el2({ value: "San Francisco, CA", attrs: { name: "loc_search" } });
  const box = boxOf([el2({ value: "San Francisco, CA" })]);
  assert.equal(v.verifyValue(visible, "San Francisco, CA", box), v.OK);
});

test("an empty box with an empty twin is missing", () => {
  const v = verifier();
  assert.equal(v.verifyValue(el2({ value: "", attrs: { name: "loc_search" } }),
                             "San Francisco, CA", boxOf([el2({ value: "" })])), v.NONE);
});

test("two hidden inputs are ambiguous, so no twin is claimed and we fall back to the element", () => {
  // Guessing which hidden field is the committed one is worse than admitting we cannot tell.
  const v = verifier();
  const box = boxOf([el2({ value: "" }), el2({ value: "" })]);
  assert.equal(v.committedTwin(el2({ attrs: { name: "x" } }), box), undefined);
  assert.equal(v.verifyValue(el2({ value: "typed", attrs: { name: "x" } }), "typed", box), v.OK);
});

test("a name-matched hidden twin found on the page counts, with no box at all", () => {
  const v = verifier([el2({ value: "San Francisco, CA" })]);
  assert.equal(v.verifyValue(el2({ value: "San Francisco, CA", attrs: { name: "loc_search" } }),
                             "San Francisco, CA", null), v.OK);
});

test("a value the field reformatted still counts — a phone mask is not a failure", () => {
  const v = verifier();
  assert.equal(v.verifyValue(el2({ value: "(415) 555-0134" }), "(415) 555-0134", null), v.OK);
});

test("a field holding something else is entered, never verified", () => {
  const v = verifier();
  assert.equal(v.verifyValue(el2({ value: "totally different text" }), "Sandeep Gupta", null), v.SOFT);
});

test("an unchecked control is never reported as set", () => {
  const v = verifier();
  assert.equal(v.verifyChecked(el2({ checked: false })), v.SOFT);
  assert.equal(v.verifyChecked(el2({ checked: true })), v.OK);
});


// ── what the combobox rules actually promise ─────────────────────────────────────────────────────

test("a combobox we typed into but never PICKED from is not verified, however right it reads", () => {
  const v = verifier();
  const el = el2({ value: "San Francisco, CA (Hybrid)" });
  assert.equal(v.verifyValue(el, "San Francisco, CA (Hybrid)", null, { isCombo: true }), v.SOFT);
});

test("a combobox IS verified once one of its own options was clicked", () => {
  const v = verifier();
  const el = el2({ value: "San Francisco, CA (Hybrid)" });
  assert.equal(v.verifyValue(el, "San Francisco, CA (Hybrid)", null, { isCombo: true, clicked: true }), v.OK);
});

test("a CLOSED option list is not proof: a list filtered to no matches is closed too", () => {
  // the trap this replaced — "Austin, TX" has no option, the list renders nothing, and the box still
  // shows what we typed. Without a click there is nothing to stand on.
  const v = verifier([], { listOpen: false });
  const el = el2({ value: "Austin, TX (On-site)" });
  assert.equal(v.verifyValue(el, "Austin, TX (On-site)", null, { isCombo: true }), v.SOFT);
});

test("a node the page has thrown away is unconfirmed, whatever it still reads", () => {
  const v = verifier([], { detached: true });
  assert.equal(v.verifyValue(el2({ value: "Sandeep Gupta" }), "Sandeep Gupta", null), v.SOFT);
});

test("an aria-checked widget counts as set — Ashby's Yes/No pair has no .checked at all", () => {
  const v = verifier();
  const btn = { getAttribute: (n) => (n === "aria-checked" ? "true" : null), closest: () => null };
  assert.equal(v.verifyChecked(btn), v.OK);
  const off = { getAttribute: () => "false", closest: () => null };
  assert.equal(v.verifyChecked(off), v.SOFT);
});
