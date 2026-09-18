# Roster Apply — local runner (CLI)

Fills a reviewed Roster application **plan** in a real browser on **your own machine**, using
[Playwright](https://playwright.dev). Because it runs locally — your own IP, your own browser session —
it works on ATS forms that block a datacenter/headless browser, and it drives fields with real
keystrokes, real file uploads and proper waiting (far more reliably than a page-injected script).

The plan itself is built and **safety-gated on the Roster server**: eligibility facts (work
authorization, sponsorship, visa, salary, years, degree) are filled from your profile only and never
model-guessed; drafted free-text is span-checked against your résumé; a blacklist, per-ATS rate limits
and a knock-out pre-scan run before anything is prepared. This runner only *executes* that plan.

**It never submits unless you pass `--submit`, and never while a required or eligibility field is still
open.** By default it fills, tells you what needs you, and leaves the browser open for you to review and
submit.

## Install

```bash
unzip roster-apply-cli.zip && cd roster-apply-cli
npm install            # also downloads the Chromium Playwright uses (postinstall)
```

Requires Node 18+.

## Connect

Copy your token from Roster → **Account → Roster Apply → Copy token**, then:

```bash
node apply.mjs login <token>
```

(Or set `ROSTER_TOKEN` in the environment. `ROSTER_BASE` overrides the server URL.)

## Use

```bash
node apply.mjs list                 # your prepared applications
node apply.mjs fill <id>            # open the form, fill it, STOP before submit
node apply.mjs fill <id> --submit   # fill, then submit (only if nothing is flagged)
node apply.mjs plan <apply-url>     # ask Roster to plan a URL first (add --fill to fill right away)
```

A Chromium window opens, the runner fills each field (`.` = filled, `!` = needs you), prints what still
needs you, and leaves the window open. Review it, then submit — or re-run with `--submit`.

## What it will not do

- Touch CAPTCHA/password fields, or any eligibility fact your profile didn't state (those are left for
  you — the runner never guesses "authorized to work" or "8 years of X").
- Submit while a required field is empty or an eligibility field is unconfirmed.
- Send anything anywhere except Roster (to fetch your plan + résumé) and the ATS form you open.
