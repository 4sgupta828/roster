"""GUIDED INTAKE service (docs/specs/guided-intake.md §3): one turn at a time — direction → artifact →
completeness read → compile → questions (gaps, required keys, optional keys) → READY. The kernel decides
WHETHER to ask (`roster_kernel.facets.intake`), the vertical supplies the words and the checklist
(`roster_vertical.intake`), a model maps a typed reply onto the pending question (ONE small JSON call per typed
turn; a chip tap costs nothing). Stateless: the state rides the request; the artifact text is capped."""
from __future__ import annotations

import asyncio
import re
from typing import Awaitable, Callable

from roster_kernel.facets import Contract, validate_contract
from roster_kernel.facets.intake import IntakeState, Question, apply_answer, next_question, search_now, spread

TRANSCRIPT_CAP, MSG_CAP, ARTIFACT_CAP = 40, 2000, 6000
_URL_RX = re.compile(r"https?://\S+")


class IntakeService:
    def __init__(self, *, schema, llm_json: Callable[[str, str], dict], counts_fn: Callable[[str, dict], Awaitable[dict]],
                 compile_fn: Callable[..., Contract], profile_fn: Callable[[dict | None], Awaitable[dict | None]] | None = None,
                 jd_fetch_fn: Callable[[str], str] | None = None, briefs_fn=None, draft_fn=None, redraft_fn=None, index_aware_fn=None):
        self.schema = schema
        self.llm_json = llm_json
        self.counts_fn = counts_fn
        self.compile_fn = compile_fn
        self.profile_fn = profile_fn
        self.jd_fetch_fn = jd_fetch_fn
        self.briefs_fn = briefs_fn          # async (user) → the account's saved briefs (JDs) — the "which JD" question
        self.draft_fn = draft_fn            # async (role_text, context) → a JD draft (api.jd_draft.build_jd_draft, app-wired)
        self.redraft_fn = redraft_fn        # async (draft, change) → the draft with the change applied
        self.index_aware_fn = index_aware_fn  # async (contract, kind=, user_keys=, place_or_mode=) → (contract, notes): spec §12 step 1

    # ---------------- helpers ----------------
    def _vocab(self):
        from roster_vertical import intake as V
        return V

    async def _model(self, system: str, user: str) -> dict:
        return await asyncio.to_thread(self.llm_json, system, user)

    def _labeled(self, key: str, options: list) -> list:
        V = self._vocab()
        return [[v, V.option_label(key, v), int(n)] for v, n in options]

    def _question_payload(self, q: Question, st: IntakeState) -> dict:
        V = self._vocab()
        artifact = V.ARTIFACT_FOR.get(st.direction, "")
        key = q.name if q.kind == "key" else ((V.item(artifact, q.name).key if V.item(artifact, q.name) else None) or "")
        words = V.question_for(q.kind, q.name, artifact if q.kind == "item" else st.direction)
        it = V.item(artifact, q.name) if q.kind == "item" else None
        payload = {"kind": q.kind, "name": q.name, "key": key, "words": words, "hint": (it.hint if it else ""),
                   "options": self._labeled(key, q.options) if key else [], "free_text": True, "klass": q.klass}
        if q.name == "comp" or key == "comp":
            payload["decline"] = V.QUESTION_WORDS["comp"]["decline"]
        return payload

    async def _counts(self, st: IntakeState) -> dict:
        V = self._vocab()
        kind = V.SEARCH_KIND.get(st.direction, "job")
        must = (st.contract or {}).get("must") or {}
        try:
            return await self.counts_fn(kind, must) or {}
        except Exception:   # noqa: BLE001 — counts are an aid; the intake proceeds without options
            return {}

    def _pool(self, counts: dict) -> int:
        for key in ("level", "field", "work_type", "work_mode"):
            if counts.get(key):
                return int(sum(int(v) for v in counts[key].values()))
        return 0

    def _advice(self, st: IntakeState, counts: dict) -> list[str]:
        V = self._vocab()
        nav = [k for k in self.schema.for_kind(V.SEARCH_KIND.get(st.direction, "job")) if k.navigable and k.type.value != "set"]
        scored = []
        for k in nav:
            s, known = spread(counts.get(k.key))
            if known >= 0.5 and s >= 0.3:
                scored.append((s, k.label.lower()))
        scored.sort(reverse=True)
        out = []
        if scored:
            out.append("This pool splits most on " + " and ".join(n for _, n in scored[:2]) + " — use those chips first.")
        unknowns = [k.label.lower() for k in nav if counts.get(k.key) and spread(counts.get(k.key))[1] < 0.5]
        if unknowns:
            out.append("Mostly unknown here: " + ", ".join(unknowns[:3]) + " — don't filter on those yet.")
        if st.direction == "job":
            out.append("Open 🚀 Apply on a card for the verified fit; save the Job Map and set weekly to catch new roles.")
        else:
            out.append("Turn on 'must have repos / papers' for proof of work; use the review states on each card.")
        return out

    def _next(self, st: IntakeState, counts: dict) -> Question | None:
        V = self._vocab()
        artifact = V.ARTIFACT_FOR.get(st.direction, "")
        # the kernel's option counts for an ITEM come from its contract key
        counts_for = dict(counts)
        for it in V.CHECKLIST.get(artifact, ()):
            if it.key and it.key in counts and it.name not in counts_for:
                counts_for[it.name] = counts[it.key]
        return next_question(st, required_items=V.required_items(artifact), required_keys=V.REQUIRED_KEYS.get(st.direction, []),
                             optional_keys=V.OPTIONAL_KEYS.get(st.direction, []), counts=counts_for)

    def _apply(self, st: IntakeState, q: Question, value, *, mode: str | None = None) -> IntakeState:
        V = self._vocab()
        artifact = V.ARTIFACT_FOR.get(st.direction, "")
        if q.kind == "item":
            it = V.item(artifact, q.name)
            key, m = (it.key if it else None), (mode or (it.mode if it else "must"))
            if key and value is not None:
                vals = value if isinstance(value, (list, tuple)) else [value]
                legal = [v for v in vals if self.schema.validate_value(key, str(v)) is not None]
                if not legal:
                    return apply_answer(st, q, value, contract_key=None)      # free text that is no legal token stays an artifact answer
                return apply_answer(st, q, legal, contract_key=key, mode=m)
            return apply_answer(st, q, value, contract_key=key, mode=m)
        m = mode or ("must" if q.klass == "required" else "prefer")
        if value is None:
            return apply_answer(st, q, None)
        vals = value if isinstance(value, (list, tuple)) else [value]
        legal = [v for v in vals if self.schema.validate_value(q.name, str(v)) is not None]
        if not legal:
            return apply_answer(st, q, None)                                    # an illegal value never constrains
        return apply_answer(st, q, legal if len(legal) > 1 else legal[0], mode=m)

    def intent_text(self, st: IntakeState, opening: str) -> str:
        """The semantic text the search runs on: the user's opening words + what the conversation established
        (role / title, field, skills, specialty, location) — never the artifact body (spec §0.3: facts feed the
        checklist; the search follows the intent)."""
        V = self._vocab()
        a = st.answers or {}
        def one(k):
            v = a.get(k)
            if isinstance(v, (list, tuple)):
                v = ", ".join(str(x) for x in v[:6])
            return str(v or "").strip()
        parts = [str(opening or "").strip()[:200]]
        # a job seeker's CURRENT title is a fact about them, not the ask ("Founder in Residence" pulled founder roles);
        # it only stands in when the user gave no words of their own
        keys = ("title", "field", "skills", "must_skills", "specialty", "location", "target_level") if (parts[0] and st.direction == "job") \
            else ("title", "current_role", "field", "skills", "must_skills", "specialty", "location", "target_level")
        for k in keys:
            v = one(k)
            if v.lower() in ("not stated", "unknown", "undisclosed", "n/a", "none", "prefer not to say"):
                continue
            if v and v.lower() not in parts[0].lower():
                parts.append(V.option_label(k, v) if k in ("field", "target_level") else v)
        return ". ".join(x for x in parts if x)[:400]

    def _with_intent(self, st: IntakeState, opening: str) -> dict:
        c = Contract.from_dict(st.contract)
        c.text = self.intent_text(st, opening) or c.text[:200]
        # LEVEL centres the ranking (a preference, never a gate — the product's standing rule); the answer may
        # have landed as a must or prefer through the checklist / key question
        lv = None
        for section in (c.must, c.prefer):
            vals = section.get("level")
            if isinstance(vals, list) and vals:
                lv = lv or str(vals[0])
        if lv and self.schema.key("level") is not None and self.schema.validate_value("level", lv):
            c.must.pop("level", None)
            c.center = {"key": "level", "value": lv, "span": 1}
        return c.to_dict()

    async def _ready(self, st: IntakeState, counts: dict, transcript: list, note: str = "") -> dict:
        V = self._vocab()
        st.contract = self._with_intent(st, str(getattr(self, "_opening", "") or ""))
        c = Contract.from_dict(st.contract)
        notes = list(getattr(self, "_notes", []) or [])
        if self.index_aware_fn is not None:
            try:
                kind = V.SEARCH_KIND.get(st.direction, "job")
                c, n2 = await self.index_aware_fn(c, kind=kind, user_keys=self.user_keys(st), place_or_mode=bool(getattr(self, "_place_or_mode", False)))
                st.contract = c.to_dict()
                seen = {(x.get("rule"), x.get("key")) for x in notes}
                notes += [x for x in n2 if (x.get("rule"), x.get("key")) not in seen]
                counts = await self._counts(st)                       # the pool may have moved
            except Exception:   # noqa: BLE001
                pass
        errs = validate_contract(c, self.schema)
        if errs:                                                                 # never hand off an illegal contract
            c = Contract(kind=c.kind, text=c.text, scope=c.scope, limit=c.limit); st.contract = c.to_dict()
        understood = V.understood_words(st.direction, st.contract, st.answers)
        pool = self._pool(counts)
        diagnosis = None
        if pool < 50 and (c.must or {}) and self.counts_fn is not None:
            try:
                from roster_kernel.facets import diagnose_musts
                diagnosis = await diagnose_musts(c, self.counts_fn, self.schema)
            except Exception:   # noqa: BLE001
                diagnosis = None
        # U DIAGNOSTICS: when the pool is small or weak, what the user's OWN filters cost (never relaxed for them)
        u_diag = None
        if diagnosis and self.user_keys(st):
            uk = self.user_keys(st)
            u_diag = [k for k in (diagnosis.get("keys") or []) if k.get("key") in uk]
        return {"direction": st.direction, "search_kind": V.SEARCH_KIND.get(st.direction), "understood": understood, "contract": st.contract,
                "counts": counts, "pool": pool, "diagnosis": diagnosis, "u_diagnostics": u_diag, "notes": notes, "artifact": dict(st.artifact), "advice": self._advice(st, counts),
                "checklist": dict(st.checklist), "answers": dict(st.answers), "note": note,
                "offer_improve": any(v == "answered" for v in st.checklist.values()),
                "transcript_audit": transcript[-TRANSCRIPT_CAP:]}

    # ---------------- the turn ----------------
    async def step(self, *, state: dict | None = None, message: str = "", direction: str | None = None, answer: dict | None = None,
                   search_now: bool = False, attachments_text: list[str] | None = None, user: dict | None = None) -> dict:
        V = self._vocab()
        state = dict(state or {})
        st = IntakeState.from_dict(state.get("kernel") or {})
        transcript = [m for m in (state.get("transcript") or []) if isinstance(m, dict)][-TRANSCRIPT_CAP:]
        pending = state.get("pending") or None
        artifact_text = str(state.get("artifact_text") or "")
        draft = state.get("draft") or None
        message = (message or "").strip()
        opening = str(state.get("opening") or "")
        self._opening = opening
        state_notes = list(state.get("notes") or [])
        self._notes = list(state_notes)
        self._place_or_mode = bool(state.get("place_or_mode"))
        if answer is not None and not message:
            # a chip answer is a user turn too (the FE shows it; the audit transcript must agree)
            av = answer.get("value")
            label = ("Prefer not to say" if av in (None, "", "__decline__") else ", ".join(str(x) for x in av) if isinstance(av, (list, tuple)) else str(av))
            if str(answer.get("name") or "") == "jd" and label in ("accept", "save"):
                label = "Use this JD"
            transcript.append({"role": "user", "text": label.replace("_", " ")[:MSG_CAP]})
        elif direction and not message and not st.direction:
            transcript.append({"role": "user", "text": "I'm looking for a role" if direction == "job" else "I'm hiring"})
        if message:
            transcript.append({"role": "user", "text": message[:MSG_CAP]})
            if not opening and len(message) < 400:
                opening = message                                      # the user's own words for the search
                self._opening = opening

        def pack(stage: str, question: dict | None = None, ready: dict | None = None, note: str = "") -> dict:
            if question:
                transcript.append({"role": "assistant", "text": question["words"][:MSG_CAP]})
            if ready:
                transcript.append({"role": "assistant", "text": ready["understood"][:MSG_CAP]})
                ready["transcript_audit"] = transcript[-TRANSCRIPT_CAP:]
            st.stage = stage if stage != "questions" else st.stage
            return {"stage": stage, "question": question, "ready": ready, "note": note,
                    "state": {"kernel": st.to_dict(), "transcript": transcript[-TRANSCRIPT_CAP:], "pending": question, "artifact_text": artifact_text[:ARTIFACT_CAP], "draft": draft, "opening": opening[:400],
                              "notes": list(getattr(self, "_notes", []) or state_notes or [])[:8], "place_or_mode": bool(getattr(self, "_place_or_mode", False))}}

        # SEARCH NOW: ready with what is known, from any stage
        if search_now:
            st2 = search_now_state(st)
            counts = await self._counts(st2) if st2.contract else {}
            st = st2
            return pack("ready", ready=await self._ready(st, counts, transcript))

        # 1) DIRECTION
        if not st.direction:
            d = (direction or "").strip().lower()
            if d not in V.DIRECTIONS and message:
                try:
                    read = await self._model(V.direction_prompt(), message[:600])
                    d = V.DIRECTION_TOKENS.get(str(read.get("direction") or "").lower(), "")
                except Exception:   # noqa: BLE001
                    d = ""
            if d not in V.DIRECTIONS:
                q = {"kind": "direction", "name": "direction", "key": "", "words": "Are you looking for a role, or hiring for one?", "hint": "",
                     "options": [["job", "I'm looking for a role", 0], ["candidate", "I'm hiring", 0]], "free_text": True, "klass": ""}
                return pack("direction", question=q)
            st.direction = d
            st.stage = "artifact"
            st.artifact = {"kind": V.ARTIFACT_FOR[d], "status": "missing"}

        # 2) ARTIFACT
        if st.artifact.get("status") != "present":
            text, source, brief_id = "", "", None
            av = str((answer or {}).get("value") or "") if answer and str((answer or {}).get("name") or "") in ("jd", "profile") else ""
            if av.startswith("brief:") and self.briefs_fn is not None:
                # a SAVED JD picked from the account (spec §2.2: several hires → "which JD?")
                try:
                    briefs = await self.briefs_fn(user) or []
                except Exception:   # noqa: BLE001
                    briefs = []
                b = next((x for x in briefs if str(x.get("id")) == av[6:]), None)
                if b and b.get("text"):
                    text, source, brief_id = str(b["text"]), "saved", int(b["id"])
            elif av == "draft" and st.artifact.get("kind") == "jd" and self.draft_fn is not None:
                if len(opening) >= 40:
                    # the opening words already name the role ("hire a CTO for my startup to lead a 10–15 person team…"):
                    # draft from them now instead of asking again
                    try:
                        draft = await self.draft_fn(opening, {"your words": opening})
                    except Exception:   # noqa: BLE001
                        draft = None
                    if draft:
                        return pack("artifact", question=self._draft_question(draft))
                q = {"kind": "draft_context", "name": "jd", "key": "", "words": V.DRAFT_CONTEXT_WORDS, "hint": "one or two sentences is plenty",
                     "options": [], "free_text": True, "klass": ""}
                return pack("artifact", question=q)
            elif av == "accept" and draft and draft.get("text"):
                text, source = str(draft["text"]), "drafted"
                brief_id = (answer or {}).get("brief_id")
            elif pending and pending.get("kind") == "draft_context" and message and self.draft_fn is not None:
                role_text = (opening + ". " + message).strip(". ") if opening and opening != message else message   # never the reply alone ("Keep going")
                try:
                    draft = await self.draft_fn(role_text, {"your words": opening, "added": message} if opening else {"your words": message})
                except Exception:   # noqa: BLE001
                    draft = None
                if draft and draft.get("no_role"):
                    q = {"kind": "draft_context", "name": "jd", "key": "", "words": "I need at least the role to draft from — the title and level, e.g. “senior backend engineer, payments, SF or remote”. What is the hire?",
                         "hint": "", "options": [], "free_text": True, "klass": ""}
                    return pack("artifact", question=q)
                if draft:
                    return pack("artifact", question=self._draft_question(draft))
                text, source = role_text, "described"                                # no draft → the words are the JD for now
            elif pending and pending.get("kind") == "draft" and message and draft and self.redraft_fn is not None:
                try:
                    draft = await self.redraft_fn(draft, message)
                except Exception:   # noqa: BLE001
                    pass
                return pack("artifact", question=self._draft_question(draft))
            if not text:
                text, source = await self._find_artifact(st, message, attachments_text or [], user, pending)
            if not text:
                q = await self._artifact_question_for(st, user)
                return pack("artifact", question=q)
            artifact_text = text[:ARTIFACT_CAP]
            st.artifact = {"kind": st.artifact["kind"], "status": "present", "source": source, "chars": len(artifact_text)}
            if brief_id is not None:
                st.artifact["brief_id"] = int(brief_id)
            await self._read_completeness(st, artifact_text)
            await self._compile(st, artifact_text, message)
            st.contract = self._with_intent(st, opening)
            state_notes = list(getattr(self, "_notes", []) or [])
            st.stage = "questions"
            pending = None
            message = ""                                                       # the artifact text is not an answer

        # 3) QUESTIONS — apply the pending answer (tap or typed), then the next gap
        note = ""
        if pending and (answer is not None or message):
            q = Question(kind=pending["kind"], name=pending["name"], options=[tuple(o[:1]) + (o[-1],) for o in pending.get("options") or []], klass=pending.get("klass") or "")
            if answer is not None:
                val = answer.get("value")
                st = self._apply(st, q, (None if val in (None, "", "__decline__") else val))
            else:
                try:
                    artifact = V.ARTIFACT_FOR.get(st.direction, "")
                    read = await self._model(V.turn_prompt(st.direction), f"QUESTION ({q.kind} {q.name}): {pending['words']}\nREPLY: {message}")
                    answers = dict(read.get("answers") or {})
                    key_of = pending.get("key") or ""
                    val = answers.pop(q.name, answers.pop(key_of, "__none__") if key_of else "__none__")
                    st = self._apply(st, q, None if val in ("__none__", None, "") else val)
                    # anything else the reply clearly stated lands too (items or keys not yet asked)
                    for name, v in answers.items():
                        if v in (None, "") or name in st.asked:
                            continue
                        it = V.item(artifact, name)
                        if it is not None:
                            st = self._apply(st, Question(kind="item", name=name, klass="gaps"), v)
                            st.counts_asked["gaps"] = max(0, st.counts_asked.get("gaps", 0) - 1)       # volunteered, not asked
                        elif self.schema.key(name) is not None:
                            st = self._apply(st, Question(kind="key", name=name, klass="required"), v)
                            st.counts_asked["required"] = max(0, st.counts_asked.get("required", 0) - 1)
                except Exception:   # noqa: BLE001 — fail-safe READY (spec §3): never a dead end
                    counts = await self._counts(st)
                    st = search_now_state(st)
                    return pack("ready", ready=await self._ready(st, counts, transcript, note="I couldn't read that reply, so here is the search with what I have — adjust it with the chips."))
        counts = await self._counts(st)
        q = self._next(st, counts)
        if q is None:
            return pack("ready", ready=await self._ready(st, counts, transcript))
        return pack("questions", question=self._question_payload(q, st))

    async def improve(self, *, state: dict) -> dict:
        """A fuller artifact from ONLY the original text + the conversation's answers (spec §2.2); `added` lists the
        lines that came from the answers, each verified to appear in the text. Nothing is saved here."""
        V = self._vocab()
        st = IntakeState.from_dict((state or {}).get("kernel") or {})
        kind = st.artifact.get("kind") or V.ARTIFACT_FOR.get(st.direction, "profile")
        original = str((state or {}).get("artifact_text") or "")
        answers = {k: v for k, v in (st.answers or {}).items() if v not in (None, "", []) and st.checklist.get(k) in ("answered", None)}
        user = "ORIGINAL:\n" + original[:ARTIFACT_CAP] + "\n\nANSWERS:\n" + "\n".join(f"{k.replace('_', ' ')}: {v if not isinstance(v, list) else ', '.join(map(str, v))}" for k, v in answers.items())
        try:
            out = await self._model(V.improve_prompt(kind), user)
        except Exception as e:   # noqa: BLE001
            return {"kind": kind, "text": "", "added": [], "error": f"model: {str(e)[:120]}"}
        text = str(out.get("text") or "").strip()
        added = [str(a).strip() for a in (out.get("added") or []) if str(a).strip() and str(a).strip() in text]
        title = str(answers.get("title") or answers.get("current_role") or "")[:120]
        return {"kind": kind, "text": text, "added": added, "title": title, "answers": answers}

    # ---------------- artifact discovery ----------------
    async def _find_artifact(self, st: IntakeState, message: str, attachments_text: list[str], user: dict | None, pending: dict | None) -> tuple[str, str]:
        kind = st.artifact.get("kind")
        att = "\n\n".join(t for t in attachments_text if t and t.strip()).strip()
        if att:
            return att, "attachment"
        if kind == "profile":
            if self.profile_fn is not None and user:
                try:
                    prof = await self.profile_fn(user) or {}
                except Exception:   # noqa: BLE001
                    prof = {}
                txt = str(prof.get("_resume_text") or "").strip()
                if not txt and prof:
                    txt = " | ".join(str(prof.get(k) or "") for k in ("current_title", "summary", "skills", "location") if prof.get(k)).strip()
                if txt:
                    return txt, "on_file"
            if message and len(message) >= 40 and pending and pending.get("kind") == "artifact":
                return message, "described"
            if message and len(message) >= 120:
                return message, "described"
            return "", ""
        # a JD
        m = _URL_RX.search(message or "")
        if m and self.jd_fetch_fn is not None:
            try:
                txt = await asyncio.to_thread(self.jd_fetch_fn, m.group(0))
            except Exception:   # noqa: BLE001
                txt = ""
            if txt and len(txt) >= 200:
                return txt, "link"
        if message and len(message) >= 200:
            return message, "pasted"
        if message and len(message) >= 40 and pending and pending.get("kind") == "artifact":
            return message, "described"
        return "", ""

    def _draft_question(self, draft: dict) -> dict:
        n = len(draft.get("peers") or [])
        words = (f"Here's a draft built from {n} peer postings and your words — every line shows where it came from. Ask for changes in words, or use it."
                 if n and not draft.get("sparse") else "Too few peer postings for this role to find a centre, so this draft is from your words only. Ask for changes, or use it.")
        return {"kind": "draft", "name": "jd", "key": "", "words": words, "hint": "", "draft": draft, "free_text": True, "klass": "",
                "options": [["accept", "Use this JD", 0], ["save", "Save to my account & use", 0]]}

    async def _artifact_question_for(self, st: IntakeState, user: dict | None) -> dict:
        q = self._artifact_question(st)
        if st.artifact.get("kind") == "jd" and self.briefs_fn is not None and user:
            try:
                briefs = [b for b in (await self.briefs_fn(user) or []) if b.get("kind", "jd") == "jd"]
            except Exception:   # noqa: BLE001
                briefs = []
            if briefs:
                q["words"] = "Which hire is this for? Pick a saved job description, or start a new role."
                q["options"] = [[f"brief:{b['id']}", str(b.get("title") or b.get("role_key") or "saved JD")[:60], 0] for b in briefs[:6]] + [["new", "A new role", 0]] + q["options"]
        if st.artifact.get("kind") == "jd" and self.draft_fn is not None and not any(o[0] == "draft" for o in q["options"]):
            q["options"].append(["draft", "Draft one with me", 0])
        return q

    def _artifact_question(self, st: IntakeState) -> dict:
        if st.artifact.get("kind") == "profile":
            return {"kind": "artifact", "name": "profile", "key": "", "words": "I'll work from your résumé. Attach it, or describe yourself in a few lines — title, years, what you build, where.",
                    "hint": "no résumé on file yet", "options": [["attach", "Attach my résumé", 0], ["describe", "I'll describe myself", 0]], "free_text": True, "klass": ""}
        return {"kind": "artifact", "name": "jd", "key": "", "words": "I'll work from the job description. Paste it, drop a link to the posting, or describe the role in a few lines.",
                "hint": "no JD on file yet", "options": [["paste", "Paste the JD", 0], ["link", "Link to the posting", 0], ["describe", "Describe the role", 0]], "free_text": True, "klass": ""}

    async def _read_completeness(self, st: IntakeState, text: str) -> None:
        V = self._vocab()
        artifact = st.artifact.get("kind", "")
        items = [it.name for it in V.CHECKLIST.get(artifact, ())]
        try:
            read = await self._model(V.completeness_prompt(artifact), text[:ARTIFACT_CAP])
        except Exception:   # noqa: BLE001 — no read → everything required is asked (the safe side)
            read = {}
        present = read.get("present") if isinstance(read.get("present"), dict) else {}
        missing = {str(x) for x in (read.get("missing") or [])}
        weak = {str(x) for x in (read.get("weak") or [])}
        st.checklist = {}
        for name in items:
            if name in present and str(present[name]).strip():
                st.checklist[name] = "present"; st.answers.setdefault(name, str(present[name])[:200])
            elif name in weak:
                st.checklist[name] = "weak"
            else:
                st.checklist[name] = "missing"

    def user_keys(self, st: IntakeState) -> set:
        """The contract keys the USER set (answers to asked questions → the item's key, or the key itself) — the
        constraints the index-aware step never relaxes."""
        V = self._vocab()
        artifact = V.ARTIFACT_FOR.get(st.direction, "")
        out = set()
        for name in st.asked:
            v = st.answers.get(name)
            if v in (None, "", []):
                continue
            it = V.item(artifact, name)
            key = (it.key if it else None) or (name if self.schema.key(name) is not None else None)
            if key:
                out.add(key)
        return out

    async def _compile(self, st: IntakeState, artifact_text: str, words: str) -> None:
        V = self._vocab()
        kind = V.SEARCH_KIND.get(st.direction, "job")
        text = (words + "\n" + artifact_text).strip() if words and words != artifact_text else artifact_text
        extras: dict = {}
        try:
            c = self.compile_fn(kind, text[:4000], limit=60, scope={}, extras=extras)
        except TypeError:
            c = self.compile_fn(kind, text[:4000], limit=60, scope={})
        except Exception:   # noqa: BLE001
            c = Contract(kind=kind, text=text[:500])
        self._place_or_mode = bool(extras.get("place_or_mode"))
        if validate_contract(c, self.schema):
            c = Contract(kind=kind, text=c.text, limit=c.limit)
        # a résumé's employer is not where the seeker wants to be; a JD's own company is not where candidates
        # come from — the intake never constrains `company` (the rail can, explicitly)
        for section in (c.must, c.prefer, c.avoid):
            section.pop("company", None)
        if st.direction == "candidate":
            # a JD names many skills; ANDing them all empties the pool. More than two skill / specialty musts → they
            # rank; the manager's explicit must-haves (the checklist question) still land as musts later. `function`
            # ranks too: a CTO's function in the people index is 'executive', a JD compiles it as 'engineering'.
            for key in ("skill", "specialty"):
                vals = c.must.get(key)
                if isinstance(vals, list) and len(vals) > 2:
                    c.prefer[key] = sorted(set(list(c.prefer.get(key) or []) + [str(v) for v in vals]))
                    del c.must[key]
            fv = c.must.pop("function", None)
            if isinstance(fv, list):
                c.prefer["function"] = sorted(set(list(c.prefer.get("function") or []) + [str(v) for v in fv]))
        # INDEX-AWARE (spec §12 step 1): measured against the index before anything runs
        self._notes = []
        if self.index_aware_fn is not None:
            try:
                c, self._notes = await self.index_aware_fn(c, kind=kind, user_keys=self.user_keys(st), place_or_mode=self._place_or_mode)
            except Exception:   # noqa: BLE001
                self._notes = []
        if st.direction == "job":
            # a résumé states FACTS (what the seeker did): they rank; only the seeker's WANTS filter (asked later)
            keep = {"field", "metro", "state", "country", "work_mode", "employment_type", "comp"}
            for key in [k for k in list(c.must.keys()) if k not in keep]:
                vals = c.must.pop(key)
                if isinstance(vals, list):
                    c.prefer[key] = sorted(set(list(c.prefer.get(key) or []) + [str(v) for v in vals]))
        st.contract = c.to_dict()


def search_now_state(st: IntakeState) -> IntakeState:
    return search_now(st)
