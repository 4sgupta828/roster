"""Résumé → profile parser — runs as a SEPARATE process (spawned by /me/profile/parse-resume), so the
heavy docling model load stays OUT of the API web process. Reads the user's stored résumé, extracts text
with DOCLING (always), asks DeepSeek to structure it into the Apply-Profile fields, and writes the result
back to roster_candidate_profile.parsed_profile (status done|failed). The FE polls and prefills the form.

Invoked as:  python -m api.resume_parser <user_id>
Env: ROSTER_CORPUS_DSN, DEEPSEEK_API_KEY. PII — never logged (only status lines)."""
import sys, os, json, tempfile, asyncio, urllib.request
import asyncpg

DS = os.environ.get("DEEPSEEK_API_KEY", "")
# flat fields (drive the Apply-Profile form + ATS autofill)
FLAT = ["first_name", "last_name", "email", "phone", "city", "region", "country", "linkedin",
        "github", "portfolio_website", "current_title", "current_company", "years_experience",
        "highest_degree", "school", "field_of_study", "grad_year"]
# structured fields (the FULL, rigorous mapping — every role & school, plus skills)
STRUCT = ["work_history", "education", "skills", "summary"]


def extract_text_docling(data: bytes, name: str) -> str:
    """DOCLING extraction (always) — write bytes to a temp file, convert, export markdown."""
    suffix = os.path.splitext(name)[1] or ".pdf"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
        tf.write(data)
        path = tf.name
    try:
        from docling.document_converter import DocumentConverter
        res = DocumentConverter().convert(path)
        return (res.document.export_to_markdown() or "")[:20000]
    finally:
        try: os.remove(path)
        except Exception: pass


def llm_fields(text: str) -> dict:
    """RIGOROUS extraction: every role and school, not just the current one. Returns flat fields for the
    form/ATS autofill PLUS structured work_history / education / skills, so nothing is dropped."""
    prompt = (
        "You are a precise résumé parser. Read the ENTIRE résumé and extract a COMPLETE, structured "
        "profile. Return ONLY a JSON object with these keys (omit a key only if truly absent):\n"
        "  first_name, last_name, email, phone, city, region, country,\n"
        "  linkedin, github, portfolio_website  (full URLs),\n"
        "  current_title, current_company        (the MOST RECENT / present role),\n"
        "  years_experience                       (integer as string; sum of professional experience),\n"
        "  highest_degree, school, field_of_study, grad_year  (the highest/most recent degree),\n"
        "  work_history: [ {title, company, location, start, end, description} ]  "
        "(EVERY role, most-recent first; end='Present' if current; description = 1-2 line summary of impact),\n"
        "  education:    [ {school, degree, field, start, end} ]  (EVERY entry),\n"
        "  skills:       [ ... ]  (all technical skills/tools/languages),\n"
        "  summary:      one-sentence professional summary.\n"
        "RULES: be exhaustive — do NOT skip older roles; preserve dates verbatim (e.g. 'Jan 2021'); "
        "infer years_experience from earliest job start to the present; no invented facts; no commentary.\n\n"
        "RÉSUMÉ:\n" + text[:24000])
    body = json.dumps({"model": "deepseek-chat", "temperature": 0, "max_tokens": 2400,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.deepseek.com/chat/completions", data=body,
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + DS})
    t = json.load(urllib.request.urlopen(req, timeout=180))["choices"][0]["message"]["content"]
    t = t[t.find("{"):t.rfind("}") + 1]
    d = json.loads(t)
    out = {}
    for k in FLAT:
        v = d.get(k)
        if v is not None and str(v).strip():
            out[k] = str(v).strip()
    for k in STRUCT:                       # keep the structured arrays/objects as-is
        v = d.get(k)
        if v:
            out[k] = v
    return out


async def run(user_id: str):
    c = await asyncpg.connect(os.environ["ROSTER_CORPUS_DSN"])
    async def fail():
        try: await c.execute("UPDATE roster_candidate_profile SET parse_status='failed', parsed_at=now() WHERE user_id=$1", user_id)
        except Exception: pass
    try:
        row = await c.fetchrow(
            "SELECT resume_name, resume_bytes FROM roster_candidate_profile WHERE user_id=$1", user_id)
        if not row or not row["resume_bytes"]:
            print("no resume", flush=True); await fail(); return
        text = await asyncio.to_thread(extract_text_docling, bytes(row["resume_bytes"]),
                                       row["resume_name"] or "resume.pdf")
        if not text.strip():
            print("empty text", flush=True); await fail(); return
        fields = await asyncio.to_thread(llm_fields, text)
        fields["_resume_text"] = text[:8000]   # keep the RAW résumé text — the richest match signal,
                                                # robust even when structured extraction is sparse
        await c.execute(
            "UPDATE roster_candidate_profile SET parse_status='done', parsed_profile=$2::jsonb, "
            "parsed_at=now() WHERE user_id=$1", user_id, json.dumps(fields))
        print(f"done: {len(fields)} fields + raw text", flush=True)
    except Exception as e:   # noqa: BLE001
        print("error:", str(e)[:120], flush=True); await fail()
    finally:
        await c.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m api.resume_parser <user_id>"); sys.exit(1)
    asyncio.run(run(sys.argv[1]))
