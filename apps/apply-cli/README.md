# Roster Apply — local runner (single binary)

Fills a reviewed Roster application **plan** in the **Chrome you already have**, driven over the Chrome
DevTools Protocol. It runs on **your** machine — your IP, your session — so it works on ATS forms that
block a datacenter/headless browser, and it drives fields with the proven React-aware fill logic (native
setter + value-tracker reset, combobox pick, real file upload).

The plan is built and **safety-gated on the Roster server**: eligibility facts (work authorization,
sponsorship, visa, salary, years, degree) are filled from your profile only and never model-guessed;
drafted free-text is span-checked against your résumé; a blacklist, per-ATS rate limits and a knock-out
pre-scan run before anything is prepared. This runner only *executes* that plan.

**It never submits unless you pass `--submit`, and never while a required or eligibility field is still
open.** By default it fills, tells you what needs you, and leaves Chrome open for you to review + submit.

- **No runtime dependencies** and **no bundled browser** — it uses your installed Chrome.
- Ships as one file (Node SEA). You need Node **only to build** it, never to run it.

## Get the binary

```bash
unzip roster-apply-cli.zip && cd roster-apply-cli
npm install        # dev-only: esbuild + postject (the build tools) — nothing at runtime
npm run build      # → dist/roster-apply  (build once per OS; needs Node 21+)
```

Then it's a standalone file — copy `dist/roster-apply` anywhere and run it (no Node needed).
(You can also just run the source with `node apply.mjs …` — also zero runtime deps.)

## Use

```bash
./dist/roster-apply login <token>        # Roster → Account → Roster Apply → Copy token
./dist/roster-apply list                 # your prepared applications
./dist/roster-apply fill <id>            # open the form in Chrome, fill it, STOP before submit
./dist/roster-apply fill <id> --submit   # fill, then submit (only if nothing is flagged)
./dist/roster-apply batch                # fill EVERY prepared application (a tab each)
./dist/roster-apply batch 12 15 --submit # fill+submit a specific set
./dist/roster-apply plan <url> --fill    # ask Roster to plan a URL and fill it now
```

Chrome opens (a throwaway profile), each field is filled, the runner prints what still needs you, and the
window stays open for your review. `batch` fills them all in separate tabs.

## What it will not do

- Touch CAPTCHA/password fields, or any eligibility fact your profile didn't state (left for you — never
  a guessed "authorized to work" / "8 years of X").
- Submit while a required field is empty or an eligibility field is unconfirmed.
- Send anything anywhere except Roster (to fetch your plan + résumé) and the ATS form you open.
