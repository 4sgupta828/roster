// Pure-logic tests for the CLI planner helpers. Run: node --test apps/apply-cli/test_plan.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { norm, pickOption, classifyField, selectorsOf, reviewFields, submitBlockedReason } from "./lib/plan.mjs";

test("pickOption: exact, decline, and contains — never a wrong yes/no", () => {
  assert.equal(pickOption(["Yes", "No"], "No"), "No");
  assert.equal(pickOption(["I am not a protected veteran", "Prefer not to say"], "prefer not to answer"), "Prefer not to say");
  assert.equal(pickOption(["United States", "Canada"], "united states"), "United States");
  assert.equal(pickOption(["Yes", "No"], "maybe"), "");
});

test("classifyField never touches CAPTCHA/eligibility-gaps, routes combo/file/choice/text", () => {
  assert.equal(classifyField({ policy: "never", kind: "text", answer: "x" }), "skip");
  assert.equal(classifyField({ needs_confirmation: true, kind: "select", answer: "Yes" }), "skip"); // unsourced eligibility fact
  assert.equal(classifyField({ kind: "file" }), "file");
  assert.equal(classifyField({ kind: "text", answer: "" }), "none");
  assert.equal(classifyField({ kind: "text", answer: "San Francisco", label: "Location" }), "combo");   // location autocomplete
  assert.equal(classifyField({ kind: "text", answer: "x", selector: "#candidate-location" }), "combo");
  assert.equal(classifyField({ kind: "select", answer: "Yes", options: ["Yes", "No"] }), "choice");
  assert.equal(classifyField({ kind: "text", answer: "Ada" }), "text");
});

test("selectorsOf splits the planner's comma list in order", () => {
  assert.deepEqual(selectorsOf({ selector: '#candidate-location, [name="location"], #location' }),
                   ["#candidate-location", '[name="location"]', "#location"]);
});

test("reviewFields separates eligibility gaps from model drafts", () => {
  const plan = [{ label: "Work auth?", needs_confirmation: true }, { label: "Why us?", source: "agent draft" }, { label: "Name", source: "profile" }];
  assert.deepEqual(reviewFields(plan), { needs: ["Work auth?"], drafts: ["Why us?"], all: ["Work auth?", "Why us?"] });
});

test("submit is blocked while a required field is empty or an eligibility fact is unconfirmed", () => {
  assert.match(submitBlockedReason([{ label: "First name", required: true, answer: "" }]), /required field/);
  assert.match(submitBlockedReason([{ label: "Work auth?", needs_confirmation: true, answer: "" }]), /eligibility field/);
  assert.equal(submitBlockedReason([{ label: "First name", required: true, answer: "Ada" }, { label: "Why", answer: "x" }]), "");
});

test("norm collapses case, punctuation and whitespace", () => {
  assert.equal(norm("  San Francisco, CA (Hybrid) "), "san francisco ca hybrid");
});
