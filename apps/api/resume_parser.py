"""Résumé → profile parser — runs as a SEPARATE process (spawned by /me/profile/parse-resume), so the
heavy docling model load stays OUT of the API web process. Reads the user's stored résumé, extracts text
with DOCLING (always), asks DeepSeek to structure it into the Apply-Profile fields, and writes the result
back to roster_candidate_profile.parsed_profile (status done|failed). The FE polls and prefills the form.

Invoked as:  python -m api.resume_parser <user_id>
Env: ROSTER_CORPUS_DSN, DEEPSEEK_API_KEY. PII — never logged (only status lines)."""
import sys, os, json, tempfile, asyncio, urllib.request
import asyncpg

DS = os.environ.get("DEEPSEEK_API_KEY", "")
FIELDS = ["first_name", "last_name", "email", "phone", "city", "region", "country", "linkedin",
          "github", "portfolio_website", "current_title", "current_company", "years_experience",
          "highest_degree", "school", "field_of_study", "grad_year"]


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
    prompt = ("Extract these fields from the résumé below. Return ONLY a JSON object containing the keys "
              "you can confidently fill (OMIT any you can't): " + ", ".join(FIELDS) + ". Rules: values are "
              "plain strings; `years_experience` a number as a string; `grad_year` a 4-digit year; "
              "`linkedin`/`github`/`portfolio_website` full URLs. No commentary.\n\nRÉSUMÉ:\n" + text[:16000])
    body = json.dumps({"model": "deepseek-chat", "temperature": 0, "max_tokens": 700,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.deepseek.com/chat/completions", data=body,
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + DS})
    t = json.load(urllib.request.urlopen(req, timeout=120))["choices"][0]["message"]["content"]
    t = t[t.find("{"):t.rfind("}") + 1]
    d = json.loads(t)
    return {k: str(v).strip() for k, v in d.items() if k in FIELDS and str(v).strip()}


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
        await c.execute(
            "UPDATE roster_candidate_profile SET parse_status='done', parsed_profile=$2::jsonb, "
            "parsed_at=now() WHERE user_id=$1", user_id, json.dumps(fields))
        print(f"done: {len(fields)} fields", flush=True)
    except Exception as e:   # noqa: BLE001
        print("error:", str(e)[:120], flush=True); await fail()
    finally:
        await c.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m api.resume_parser <user_id>"); sys.exit(1)
    asyncio.run(run(sys.argv[1]))
