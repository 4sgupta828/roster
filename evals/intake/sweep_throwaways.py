"""Delete the eval / smoke throwaway accounts (intake-eval-%, briefs-smoke-%, rail-smoke-% @roster.test) and their rows.
Runs INSIDE the API container (needs ROSTER_CORPUS_DSN):
  b64=$(base64 < evals/intake/sweep_throwaways.py | tr -d '\n')
  railway ssh --service roster-api "sh -c \"echo $b64 | base64 -d > /tmp/sweep.py && python /tmp/sweep.py\""
Never touches a real account: the match is the throwaway email patterns only."""
import os, asyncio, asyncpg
PAT = ("intake-eval-%@roster.test", "briefs-smoke-%@roster.test", "rail-smoke-%@roster.test", "nudge-smoke-%@roster.test")
TABLES = ['roster_answer_bank', 'roster_application', 'roster_brief', 'roster_bucket', 'roster_candidate_profile', 'roster_connection',
          'roster_feedback', 'roster_notification', 'roster_outreach', 'roster_saved_search', 'roster_user_pref', 'roster_user_token']
async def main():
    cx = await asyncpg.connect(os.environ["ROSTER_CORPUS_DSN"])
    ids = [r["id"] for r in await cx.fetch("select id from roster_user where email like any($1::text[])", list(PAT))]
    print("throwaway users:", len(ids))
    if not ids:
        return
    async with cx.transaction():
        # user_id may be text or uuid per table: compare as text
        for t in TABLES:
            res = await cx.execute(f"delete from {t} where user_id::text = any($1::text[])", [str(i) for i in ids])
            if not res.endswith(" 0"):
                print(" ", t, res)
        res = await cx.execute("delete from roster_user where id::text = any($1::text[])", [str(i) for i in ids])
        print("  roster_user", res)
    r = await cx.fetchrow("select count(*) n from roster_user where email like any($1::text[])", list(PAT))
    print("remaining throwaways:", r["n"])
    await cx.close()
asyncio.run(main())
