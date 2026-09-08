# 🚀 Apply — the flow from a job link to a submitted application

**Status:** reworked 2026-09-08 after four owner-reported bugs. Panel-reviewed (Codex `gpt-5.5`,
Gemini 3 Pro, plus a code-grounded pass) and measured against a real React form before any fix.

**The one rule that never moves:** Roster never submits an application and never clicks a submit
button. It reads the form, plans the answers, fills them in the user's own browser, and the USER
submits. Nothing below changes that.

---

## 1. What the owner reported

1. "it starts to ask for pasting job description, despite link again, to tailor resume"
2. "'Save my answers' is kind of confusing… what makes sense is to bolden Next: Fill and Submit"
3. "on final page I still [see] Analyze and Plan and key instructions are grayed out"
4. "the submit complains always about certain subset of fields even if filled right, because I think
   browser thinks they are still empty, because input didn't come from a human"

Two of these turned out to be worse than reported, and one turned out to have a different cause than
anyone on the panel guessed.

---

## 2. What was actually wrong

### 2.1 (1) and (3) are one bug

The job-source block — the URL input, the "…or paste the job description" textarea and the
"🎯 Analyze & plan" button — sits OUTSIDE the four tab panes, and exactly one line ever set its
visibility (`openApplyFlow`). Nothing hid it again after a successful analysis, so having pasted a
link the user still saw the app asking for the job, on every step including the last.

**Fixed:** once a job is known, the block collapses to one line — `Analyzing <url>` with a
**Change job** button that restores it. Scoped to the top-bar entry point, which is the only one that
ever showed the block.

### 2.2 (2) is not cosmetic — it was silent data loss

The extension does not read the Roster page. At fill time it fetches the plan from the **server**
(`GET /me/applications/{id}`); there is no channel between page and extension at all. So an answer
typed in step 2 was only ever used if the user first pressed **Save my answers**.

That button was the PRIMARY one and "Next: fill & submit →" was a ghost button pushed to the far
right by a `margin-left:auto`. A user following the owner's own instinct — press Next — proceeded with
their edits **discarded, silently**.

**Fixed:** answers save themselves (debounced on input, immediately on change), the Save button is
gone, the note under the pane says *"Saved automatically · the extension fills from these"* and then
*"Saved · 7 of 11 answered"*, and **Next saves before it navigates** rather than racing it. A drafted
cover letter dropped into the plan saves itself too. `saveApplicationAnswers()` is kept as a thin
alias so older call sites still work.

### 2.3 (3)'s second half: the operative screen was styled as fine print

Every word of step 4 rendered in `var(--faint, #8a93ad)` or `--muted`. **`--faint` is never defined
anywhere in the file**, so all three themes fell through to the hardcoded `#8a93ad` — low contrast on
the light background. Nothing in that pane used `--ink` or an accent.

**Fixed:** step 4 is an instruction console — a bold heading, a full-size primary
**Open the application form ↗**, and three numbered steps at full `--ink` contrast. The disclaimers
stay muted, because they are disclaimers.

### 2.4 (4): the fill bug is real, and it is NOT what "not from a human" suggests

Measured, before changing anything, by running the shipped setter against a real React-controlled
form in a headless browser:

| widget | DOM after fill | what the form actually held | naive read-back catches it? |
|---|---|---|---|
| plain text / textarea | ✓ | ✓ | — |
| native `<select>`, checkbox | ✓ | ✓ | — |
| an input that only accepts **trusted** events | ✓ | ✓ | — |
| masked phone | ✓ | ✓ | — |
| a picker that ignores typing | empty | empty | **yes** |
| **a combobox whose committed value lives in a HIDDEN input** | **✓ looks right** | **empty** | **NO** |

Two conclusions the measurement forces:

1. **The `isTrusted` theory is wrong.** `document.execCommand("insertText")` produces a *trusted*
   input event, and the strict field accepted it. More event-dancing is not the fix.
2. **The last row is the owner's bug.** The visible box reads back exactly what we typed while the
   form holds nothing — so a plain read-back verification would *also* have called it a success.

The code-grounded pass then found four ways the extension claimed success with no evidence at all:
`clickOption` returned "filled" and painted the green outline **while dispatching zero events** when a
control was already checked; the custom-combobox path returned `true` whether or not an option was
ever clicked; a click on a DIV-pretending-to-be-a-radio (every React ATS widget) dispatched only
`click`, so a site listening for `input`/`change` never heard it; and `[role=option]` was queried
against the whole `document` rather than the field's own box.

**Fixed — three outcomes, never two:**

| state | meaning | outline |
|---|---|---|
| `verified` | something readable confirms the form took it — the element itself, its hidden committed twin, `files.length`, `checked`, or the selected option | purple |
| `entered` | we set it and **cannot confirm** the form accepted it | **amber** |
| `missing` | not found on the page, or a required question with no planned answer | red |

`verifyValue()` prefers the **committed twin** (a lone hidden input sharing the field's base name, or
the one hidden input in the question's box) over the visible element, because the twin is what the
site validates. Two candidate twins is ambiguous, so no twin is claimed — guessing which hidden field
is the real one would be worse than admitting we cannot tell. A combobox now commits by clicking the
option **scoped to its own box**, and a click on a non-input dispatches `input` + `change` itself.

Also fixed while in there: a run that filled nothing reported nothing at all (so a total miss looked
like never pressing the button); the background script **assigned** rather than merged `missing`
across frames, so a frame that missed everything was dropped and a later frame erased an earlier
frame's misses; and a required question with no answer was counted in neither list.

**And the user is told.** The page banner, the extension popup and step 4 all NAME the fields that
need attention — a count sends someone hunting a forty-field form.

---

## 3. What was deliberately NOT changed

- Both panel reviewers wanted the four tabs replaced with a linear stepper. The tabs are already
  numbered 1–4 and every pane now ends in a primary "Next" button, which gives the linear path
  without taking away the ability to jump back to the fit table — and the owner asked for the flow to
  be optimised, not re-specified.
- The extension still reads the plan from the server, not from the page. Both reviewers independently
  rejected the alternative: a tab-to-tab DOM coupling is brittle and fails exactly when a user has the
  form open on another screen.
- Roster still never submits.

## 4. Tests

- `apps/extension/test_fill.mjs` — the extension's **first** tests (10). No npm dependency and no
  toolchain: this repo has no `package.json`, and a test that needs an install is a test nobody runs,
  so the five DOM calls the verifier uses are stubbed and the real code is loaded out of `content.js`.
  Run: `node --test apps/extension/test_fill.mjs`.
- `apps/api/test_apply_plan.py` — four new cases pinning what the card says after a fill: a clean fill
  says nothing, an unconfirmed field is reported like a missing one, one field reads as "1 field", and
  a long list is named-then-trimmed rather than counted.

---

## 5. The fill bug, actually found (2026-09-08, later)

§2.4 shipped honest REPORTING of a fill and claimed the setter was sound. The owner then hit the same
thing on a live Ashby form: Name accepted, **Email / LinkedIn / Phone / Desired work location /
work-eligibility all visibly filled and all rejected as "Missing entry for required field"**.

So this time the harness was built to be the form, not a proxy for it: a React-controlled page with
Ashby's `data-field-path` containers, a combobox whose committed value is separate from its search
box, Yes/No as `role=radio` BUTTONS, and **a validator that reads React state** — then the REAL
shipped `content.js` is loaded into it with a stubbed `chrome.runtime` and told to fill.

### What that found — three defects, all now fixed

1. **`clickOption` clicked the CONTAINER, not the control.** A listbox holding one option has exactly
   the option's text, and so does every wrapper around it. `pool.find(text === want)` returned the
   first match in document order — the wrapper — and clicking a wrapper fires nobody's handler. This
   is the whole combobox failure: "San Francisco, CA (Hybrid)" sat in the box with the form empty.
   Candidates are now ranked, a real control (`[role=option]`, `label`, `button`, `li`) above a
   generic box, then the deepest node.
2. **Everything was verified in the same tick it was set.** React commits on its own schedule, so a
   verification that runs immediately reads the state as it was BEFORE our event — which reported two
   working radio groups as "unconfirmed" and would have sent the reader to retype them.
3. **The setter had `execCommand` first.** Now the native prototype setter runs first, with
   `_valueTracker` reset to the previous value so React cannot treat our event as a no-op, and
   `execCommand` is kept only as a second attempt when the value did not stick. Plus a yield between
   fields, so a synchronous loop cannot race the framework's re-render.

### And two corrections to the verification itself

- **A combobox is verified ONLY by an actual option click** (or the widget's own `aria-selected` /
  chip). Two weaker rules were tried and both are unsound: reading our own text back out of the
  search box (it returns exactly what we typed while the form holds nothing), and "the option list
  has closed" — a list filtered to NO MATCHES is closed too, which is precisely the case where
  nothing was committed. Measured: asking for a value the widget has no option for now reports
  `unconfirmed`, where the closed-list rule reported `filled`.
- **`aria-checked` counts.** Ashby's Yes/No pair is two BUTTONS with no `.checked` property at all.

### The result, measured

| | before | after |
|---|---|---|
| the form's own verdict | "Missing entry for required field: Desired work location" | **accepts every required field** |
| what the extension claimed | "filled" for a field the form never took | matches the form exactly |
| a value the widget cannot commit | claimed as filled | reported `unconfirmed` |

### What is NOT resolved

**The harness never reproduced the Email / LinkedIn / Phone failure.** Plain controlled inputs update
React state correctly on every path tried — with `execCommand`, with it forced off, and across
`text`/`email`/`url`/`tel`/`number`/`search`/`password`/`date`. The panel's leading hypothesis
(Gemini; Codex's account model was 404 and it could not be consulted) is **re-render detachment**: a
synchronous loop fills nodes that a pending re-render then replaces. The yield between fields and the
re-query on verification both address that, but neither is *proven* against the real page.

So the extension now carries a **diagnostic**. Every fill records, per field: which selector matched,
the value at t=0 and t≈1s, whether the node was still attached both times, `aria-invalid`, and any
"required/missing/invalid" text the page printed beside it. "Copy diagnostics" in the popup hands it
back as JSON. The next occurrence answers the question instead of producing another guess.

### Tests
`apps/extension/test_fill.mjs` is 15 cases now (`node --test apps/extension/test_fill.mjs`), including
the combobox that must not be trusted, the closed-list trap, the detached node, and `aria-checked`.
