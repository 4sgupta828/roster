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
                 jd_fetch_fn: Callable[[str], str] | None = None):
        self.schema = schema
        self.llm_json = llm_json
        self.counts_fn = counts_fn
        self.compile_fn = compile_fn
        self.profile_fn = profile_fn
        self.jd_fetch_fn = jd_fetch_fn

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
        if value is not None and self.schema.validate_value(q.name, str(value)) is None:
            return apply_answer(st, q, None)                                    # an illegal value never constrains
        return apply_answer(st, q, value, mode=m)

    def _ready(self, st: IntakeState, counts: dict, transcript: list, note: str = "") -> dict:
        V = self._vocab()
        c = Contract.from_dict(st.contract)
        errs = validate_contract(c, self.schema)
        if errs:                                                                 # never hand off an illegal contract
            c = Contract(kind=c.kind, text=c.text, scope=c.scope, limit=c.limit); st.contract = c.to_dict()
        understood = V.understood_words(st.direction, st.contract, st.answers)
        return {"direction": st.direction, "search_kind": V.SEARCH_KIND.get(st.direction), "understood": understood, "contract": st.contract,
                "counts": counts, "pool": self._pool(counts), "artifact": dict(st.artifact), "advice": self._advice(st, counts),
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
        message = (message or "").strip()
        if message:
            transcript.append({"role": "user", "text": message[:MSG_CAP]})

        def pack(stage: str, question: dict | None = None, ready: dict | None = None, note: str = "") -> dict:
            if question:
                transcript.append({"role": "assistant", "text": question["words"][:MSG_CAP]})
            if ready:
                transcript.append({"role": "assistant", "text": ready["understood"][:MSG_CAP]})
                ready["transcript_audit"] = transcript[-TRANSCRIPT_CAP:]
            st.stage = stage if stage != "questions" else st.stage
            return {"stage": stage, "question": question, "ready": ready, "note": note,
                    "state": {"kernel": st.to_dict(), "transcript": transcript[-TRANSCRIPT_CAP:], "pending": question, "artifact_text": artifact_text[:ARTIFACT_CAP]}}

        # SEARCH NOW: ready with what is known, from any stage
        if search_now:
            st2 = search_now_state(st)
            counts = await self._counts(st2) if st2.contract else {}
            st = st2
            return pack("ready", ready=self._ready(st, counts, transcript))

        # 1) DIRECTION
        if not st.direction:
            d = (direction or "").strip().lower()
            if d not in V.DIRECTIONS and message:
                try:
                    read = await self._model(V.turn_prompt("job"), "QUESTION: Are you looking for a role, or hiring?\nREPLY: " + message)
                    d = str(read.get("direction") or "").lower()
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
            text, source = await self._find_artifact(st, message, attachments_text or [], user, pending)
            if not text:
                q = self._artifact_question(st)
                return pack("artifact", question=q)
            artifact_text = text[:ARTIFACT_CAP]
            st.artifact = {"kind": st.artifact["kind"], "status": "present", "source": source, "chars": len(artifact_text)}
            await self._read_completeness(st, artifact_text)
            self._compile(st, artifact_text, message)
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
                    return pack("ready", ready=self._ready(st, counts, transcript, note="I couldn't read that reply, so here is the search with what I have — adjust it with the chips."))
        counts = await self._counts(st)
        q = self._next(st, counts)
        if q is None:
            return pack("ready", ready=self._ready(st, counts, transcript))
        return pack("questions", question=self._question_payload(q, st))

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

    def _compile(self, st: IntakeState, artifact_text: str, words: str) -> None:
        V = self._vocab()
        kind = V.SEARCH_KIND.get(st.direction, "job")
        text = (words + "\n" + artifact_text).strip() if words and words != artifact_text else artifact_text
        try:
            c = self.compile_fn(kind, text[:4000], limit=60, scope={})
        except Exception:   # noqa: BLE001
            c = Contract(kind=kind, text=text[:500])
        if validate_contract(c, self.schema):
            c = Contract(kind=kind, text=c.text, limit=c.limit)
        st.contract = c.to_dict()


def search_now_state(st: IntakeState) -> IntakeState:
    return search_now(st)
