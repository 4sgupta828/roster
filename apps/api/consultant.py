"""GUIDED v3 — THE RECRUITING CONSULTANT (docs/specs/guided-consultant-v3.md). One planner call per turn (single pass):
code reads the documents once, computes the LEVERAGE (pool, the keys that split it, the market centre) BEFORE the call,
the planner returns ONE move, the kernel parses and gates it, code applies it. The turn path is stateless (the state
rides the request); a write-behind record keeps history. The hand-off is the ONE contract, exactly as v2's."""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Awaitable, Callable

from roster_kernel.facets import Contract, pool_from_counts
from roster_kernel.facets.brief import GAP_SOURCES, KNOWN_SOURCES, Brief, apply_brief_delta, contract_from_brief, gate_fork, leverage, parse_move, readiness

TRANSCRIPT_KEEP, TURN_CAP, DOC_CAP = 16, 300, 6000


class IntakeConsultant:
    def __init__(self, *, schema, llm_json: Callable[[str, str], dict], counts_fn: Callable[[str, dict], Awaitable[dict]],
                 slice_fn: Callable[[str, dict], Awaitable[int | None]] | None = None, profile_fn=None, stored_fn=None, briefs_fn=None,
                 draft_fn=None, index_aware_fn=None, record_fn=None, jd_fetch_fn=None, planner_llm=None, evaluate_fn=None, lines_fn=None):
        self.schema = schema
        self.llm = llm_json                   # the fast extractor: direction read, document reader
        self.planner = planner_llm or llm_json   # the BEST reasoning model: the one strategic call per turn
        self.evaluate_fn = evaluate_fn        # async (contract dict, depth=) → rows (the PREVIEW the planner judges)
        self.lines_fn = lines_fn              # async (ids) → {id: role line} for people previews
        self.counts_fn = counts_fn
        self.slice_fn = slice_fn
        self.profile_fn = profile_fn          # async (user) → parsed résumé profile ({_resume_text, …}) or None
        self.stored_fn = stored_fn            # async (user) → the apply profile record ({profile: {...}, resume: {...}}) or None
        self.briefs_fn = briefs_fn            # async (user) → saved JD briefs
        self.draft_fn = draft_fn              # async (role_text, context) → JD draft (api.jd_draft.build_jd_draft)
        self.index_aware_fn = index_aware_fn  # async (contract, kind=, user_keys=, place_or_mode=) → (contract, notes)
        self.record_fn = record_fn            # async (record dict) → None (write-behind)
        self.jd_fetch_fn = jd_fetch_fn        # (url) → text

    # ------------------------------------------------------------------ vocabulary
    @staticmethod
    def _v():
        from roster_vertical import consultant as V
        return V

    async def _model(self, system: str, user: str) -> dict:
        return await asyncio.to_thread(self.llm, system, user)

    # ------------------------------------------------------------------ the turn
    async def step(self, *, message: str = "", answer: dict | None = None, state: dict | None = None, user: dict | None = None,
                   attachments_text: list[str] | None = None, restart: bool = False, search_now: bool = False, direction_tap: str | None = None) -> dict:
        V = self._v()
        st = self._fresh() if (restart or not state or int((state or {}).get("v") or 0) != 3) else dict(state)
        if state and state.get("scope"):
            st["scope"] = dict(state["scope"])
        if restart:
            st["restarted"] = True
        message = (message or "").strip()[:4000]
        if message:
            st["transcript"].append({"role": "user", "text": message[:TURN_CAP]})
        notes: list[str] = []
        brief = Brief.from_dict(st.get("brief"))

        # 1) a chip answer to the pending question → the brief (source asked) + the option's effect
        pend = st.get("pending")
        if answer is not None and pend:
            brief, st = self._apply_answer(brief, st, pend, answer)
            st["pending"] = None
        elif message and pend and pend.get("field") and not pend.get("options_only"):
            # typed instead of tapping: the planner reads it against the pending question (below)
            pass

        if direction_tap in ("job", "candidate"):
            if brief.value("direction") != direction_tap:
                st["docs_read"] = False; st["artifact"] = {"kind": None, "status": "none"}; st["artifact_text"] = ""
            brief = brief.with_field("direction", direction_tap, "asked")
        # 1b) the side, settled by a cheap read on the first words (never guessed from a bare title list)
        if self._direction(brief, st) is None and message and not st.get("direction_read"):
            st["direction_read"] = True
            try:
                r = await self._model(V.direction_prompt(), message[:1500])
                val = str((r or {}).get("direction") or "").strip().lower()
                if val in ("job", "candidate"):
                    # a side the user literally said is a fact; a side read between the lines is an ASSUMPTION — shown, one tap to switch
                    src = "stated" if bool((r or {}).get("explicit")) else "inferred"
                    brief = brief.with_field("direction", val, src, span=str((r or {}).get("span") or message[:120])[:160])
                    if src == "inferred":
                        notes.append("direction inferred, not stated — shown as an assumption with a switch")
            except Exception as e:   # noqa: BLE001
                notes.append(f"direction read failed: {str(e)[:60]}")
            st["calls"] = int(st.get("calls") or 0) + 1
        if self._direction(brief, st) is None and not answer:
            # ambiguous words: the only question is the side (a bare title list is either)
            dq = st.get("direction_question") or {"say": "", "text": "Are you looking for a role for yourself, or hiring for your team?"}
            q = {"field": "direction", "text": dq["text"], "why": "everything else depends on it", "move": "fork", "multi": False, "free_text": True,
                 "options": [{"label": "Looking for a role", "value": "job", "effect": {}}, {"label": "Hiring", "value": "candidate", "effect": {}}]}
            st["pending"] = q; st["brief"] = brief.to_dict()
            st["transcript"].append({"role": "assistant", "text": (dq["say"] + " " + q["text"]).strip()[:TURN_CAP]})
            return self._pack(st, brief, q, say=dq["say"] or "Before anything else:", stage="question", notes=notes)
        # 2) documents: read once, before anything is asked
        if not st.get("docs_read"):
            brief, st, dn = await self._read_documents(brief, st, user, attachments_text or [], message)
            notes += dn

        # 3) steering words the planner must see: nothing special — restart/split are moves; but a bare title list stays ambiguous

        # 4) the search as it stands, its preview, the leverage and the market — computed BEFORE the planner call
        direction = self._direction(brief, st)
        kind = V.SEARCH_KIND.get(direction or "job", "job")
        contract, cnotes = self._contract(brief, st, direction or "job")
        lev, pool, market = await self._leverage(contract, kind, brief, direction)
        if getattr(self, "_last_metro_tokens", None):
            st["metro_tokens"] = list(self._last_metro_tokens)
        preview = await self._preview(contract, kind) if direction else {}

        # 5) the planner (ONE call on the best reasoning model), unless the budget is spent or the user asked to search now
        forced_ready = search_now or int(st.get("calls") or 0) >= V.MAX_PLANNER_CALLS
        if forced_ready:
            move = None
        else:
            move = await self._plan(brief, st, direction, message, pend, lev, pool, market, preview=preview)
            st["calls"] = int(st.get("calls") or 0) + 1
            notes += move.notes
            if move.move == "restart" and not restart and len([t for t in st.get("transcript") or [] if t.get("role") == "user"]) < 2:
                notes.append("restart on the first turn ignored"); move.move = "infer"
            # the question budget: any question counts; past it, the search runs with the assumptions shown
            if move.question and len(st.get("asked_fields") or []) >= V.MAX_QUESTIONS:
                notes.append("question budget spent → ready"); move.question = None; move.ready = True
            if move.question and move.question.field and move.question.field in (st.get("asked_fields") or []):
                notes.append(f"{move.question.field!r} was already asked → not again"); move.question = None; move.ready = True
            if move.move == "fork" and not move.question:
                move.ready = True                                                    # the model wanted to ask but nothing askable remains
        st["pending"] = None

        # 6) apply the move
        if move is not None:
            brief, an = apply_brief_delta(brief, move.brief_delta, allowed_fields=V.FIELD_KEYS)
            notes += an
            if move.contract_delta:
                st["overlay"] = self._merge_overlay(st.get("overlay") or {}, move.contract_delta)
            if move.move == "restart":
                st = self._fresh(); st["say"] = move.say
                st["transcript"] = [{"role": "assistant", "text": move.say[:TURN_CAP]}]
                return self._pack(st, Brief(), None, say=move.say, stage="restarted", notes=notes)
            direction = self._direction(brief, st)
            kind = V.SEARCH_KIND.get(direction or "job", "job")
            if direction and not st.get("docs_read"):
                # the side just became known: read what is on file for it before anything is asked
                brief, st, dn = await self._read_documents(brief, st, user, attachments_text or [], "")
                notes += dn
            # a fork must survive the index
            if move.question and move.question.options:
                known_metros = set(st.get("metro_tokens") or [])
                for o in move.question.options:                                     # a metro the index does not know is no option
                    for mode in ("must", "prefer", "avoid"):
                        m = (o.get("effect") or {}).get(mode) or {}
                        if known_metros and isinstance(m.get("metro"), list):
                            bad = [v for v in m["metro"] if v not in known_metros and v != "remote"]
                            if bad:
                                notes.append(f"dropped metro {bad} — not index tokens"); m["metro"] = [v for v in m["metro"] if v not in bad]
                                if not m["metro"]:
                                    del m["metro"]
            if move.question and move.question.options and move.move in ("fork", "trade_off", "challenge") and any(o.get("effect") for o in move.question.options):
                # only a fork whose options reach the CONTRACT is measured; a brief-only fork (direction, posture, mission) is asked as is
                pools = await self._probe_options(kind, brief, st, direction or "job", move.question.options)
                kept, worth, why = gate_fork(move.question.options, pools)
                if not worth and move.question.field:
                    if brief.settled(move.question.field):
                        notes.append(f"fork on {move.question.field!r} not asked: {why}"); move.question = None
                    else:                                                            # still open: ask it in words, without the dead options
                        notes.append(f"fork on {move.question.field!r} kept as free text: {why}"); move.question.options = []
                else:
                    move.question.options = kept
            if move.question:
                st["asked_fields"] = list(st.get("asked_fields") or []) + ([move.question.field] if move.question.field else [])
            if move.move == "draft" and direction == "candidate" and self.draft_fn is not None:
                brief, st, say = await self._draft(brief, st, move.say)
                st["brief"] = brief.to_dict()
                st["transcript"].append({"role": "assistant", "text": say[:TURN_CAP]})
                return self._pack(st, brief, None, say=say, stage="draft", notes=notes, direction=direction)
        st["brief"] = brief.to_dict()

        # 7) ready? — the planner judged the preview; code adds the budget and the user's "search now"
        wants_ready = bool(direction) and (forced_ready or (move is not None and move.ready) or (move is not None and not move.question and move.move in ("infer", "fork")))
        if wants_ready and direction:
            ready = await self._ready(brief, st, direction, say=(move.say if move else "I have enough to search; here is what I'm assuming."))
            st["transcript"].append({"role": "assistant", "text": ready["understood"][:TURN_CAP]})
            await self._record(st, brief, direction, stage="ready")
            return self._pack(st, brief, None, say=ready["understood"], stage="ready", notes=notes, direction=direction, ready=ready)

        # 8) a question (or a statement) back to the user
        q = None
        if move is not None and move.question:
            q = {"field": move.question.field, "text": move.question.text, "why": move.question.why, "options": [
                {"label": o["label"], "value": o.get("value", o["label"]), "effect": o.get("effect") or {}} for o in move.question.options],
                 "multi": move.question.multi, "free_text": move.question.free_text, "move": move.move}
            st["pending"] = q
        say = move.say if move else "Let me search with what I have."
        if q and self._squash(q["text"]).rstrip("?") and self._squash(q["text"]).rstrip("?") in self._squash(say):
            q_words = ""                                                             # the reply already asks it — never twice in the bubble
        else:
            q_words = (" " + q["text"]) if q else ""
        st["transcript"].append({"role": "assistant", "text": (say + q_words)[:TURN_CAP]})
        st["transcript"] = st["transcript"][-TRANSCRIPT_KEEP:]
        await self._record(st, brief, direction, stage="question" if q else "statement")
        return self._pack(st, brief, q, say=say, stage="question" if q else "statement", notes=notes, direction=direction, pool=pool)

    # ------------------------------------------------------------------ pieces
    def _fresh(self) -> dict:
        return {"v": 3, "brief": {}, "transcript": [], "pending": None, "artifact": {"kind": None, "status": "none"}, "artifact_text": "",
                "overlay": {}, "forks": 0, "asked_fields": [], "calls": 0, "docs_read": False, "record_id": hashlib.sha1(f"{time.time()}".encode()).hexdigest()[:12], "opening": ""}

    def _required(self, brief: Brief, direction: str | None) -> tuple:
        """The required fields for this brief: a remote role / seeker settles the metro."""
        V = self._v()
        req = V.REQUIRED.get(direction or "job", ())
        wm = brief.value("work_mode")
        remote = (isinstance(wm, list) and "remote" in [str(x).lower() for x in wm]) or str(wm or "").lower() == "remote"
        return tuple(k for k in req if not (remote and k in V.REMOTE_SETTLES))

    def _direction(self, brief: Brief, st: dict) -> str | None:
        d = brief.value("direction")
        return d if d in ("job", "candidate") else None

    def _apply_answer(self, brief: Brief, st: dict, pend: dict, answer: dict) -> tuple[Brief, dict]:
        val = answer.get("value")
        fld = pend.get("field") or str(answer.get("name") or "") or ""
        if not fld:
            direction = self._direction(brief, st)
            open_req = readiness(brief, self._required(brief, direction), accept_inferred=True) if direction else []
            fld = open_req[0] if open_req else ""                                  # a field-less question was about the next open thing
        if val in (None, "", "__decline__"):
            if fld:
                brief = brief.skipped(fld)
            return brief, st
        vals = val if isinstance(val, list) else [val]
        chosen = [o for o in (pend.get("options") or []) if str(o.get("value", o.get("label"))) in [str(v) for v in vals] or o.get("label") in vals]
        if fld:
            value = [str(o.get("value", o["label"])) for o in chosen] if chosen else [str(v) for v in vals]
            brief = brief.with_field(fld, value if len(value) > 1 else value[0], "asked", note=pend.get("text", "")[:120])
        for o in chosen:
            st["overlay"] = self._merge_overlay(st.get("overlay") or {}, o.get("effect") or {}, hard=True)
        st["transcript"].append({"role": "user", "text": ", ".join(o["label"] for o in chosen) if chosen else str(val)[:TURN_CAP]})
        return brief, st

    @staticmethod
    def _merge_overlay(overlay: dict, effect: dict, *, hard: bool = False) -> dict:
        out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in (overlay or {}).items()}
        for mode in ("must", "prefer", "avoid"):
            m = (effect or {}).get(mode) or {}
            tgt = mode if (hard or mode != "must") else "prefer"           # a planner's own must only ranks; an answered option may filter
            for key, vals in m.items():
                cur = out.setdefault(tgt, {}).get(key) or []
                out[tgt][key] = sorted(set(list(cur) + [str(v) for v in (vals if isinstance(vals, list) else [vals])]))
        if isinstance((effect or {}).get("center"), dict):
            out["center"] = dict(effect["center"])
        return out

    @staticmethod
    def _metro_token(value, tokens: list[str] | None):
        """A place as the index writes it: 'New York' → new_york when that token exists; a token already known stays; else None
        (the words still ride the search text). Deterministic spelling only — never a meaning guess."""
        if value in (None, "", []):
            return None
        vals = value if isinstance(value, list) else [value]
        out = []
        for v in vals:
            t = str(v).strip().lower().replace("-", " ").replace(",", " ")
            t = "_".join(t.split())
            if not tokens or t in tokens or t == "remote":
                out.append(t)
        return out or None

    def _contract(self, brief: Brief, st: dict, direction: str) -> tuple[Contract, list[str]]:
        V = self._v()
        kind = V.SEARCH_KIND.get(direction, "job")
        text = self._intent_text(brief, st, direction)
        mapping = dict(V.contract_mapping(direction))
        m = brief.get("metro")
        if m is not None and m.value not in (None, "", []) and m.source not in GAP_SOURCES:
            tok = self._metro_token(m.value, st.get("metro_tokens"))
            if tok is None:
                mapping.pop("metro", None)                                          # not a place the index knows: the words carry it
            else:
                brief = brief.with_field("metro", tok if len(tok) > 1 else tok[0], m.source, span=m.span, note=m.note)
        c, notes = contract_from_brief(brief, mapping, self.schema, kind=kind, text=text, scope=dict(st.get("scope") or {}))
        ov = st.get("overlay") or {}
        for mode in ("must", "prefer", "avoid"):
            for key, vals in (ov.get(mode) or {}).items():
                if self.schema.key(key) is None:
                    continue
                sec = getattr(c, mode)
                sec[key] = sorted(set(list(sec.get(key) or []) + [str(v) for v in vals]))
        if isinstance(ov.get("center"), dict) and ov["center"].get("key"):
            c.center = {"key": str(ov["center"]["key"]), "value": str(ov["center"].get("value")), "span": int(ov["center"].get("span") or 1)}
        for section in (c.must, c.prefer, c.avoid):
            section.pop("company", None)                                            # never the user's own employer / the JD's company
            vals = section.get("metro")
            if isinstance(vals, list) and any(str(v).lower() in ("remote", "anywhere", "wfh") for v in vals):
                section["metro"] = [v for v in vals if str(v).lower() not in ("remote", "anywhere", "wfh")]
                if not section["metro"]:
                    del section["metro"]
                if kind == "job":
                    c.must.setdefault("work_mode", [])
                    if "remote" not in c.must["work_mode"]:
                        c.must["work_mode"].append("remote")
                elif (st.get("scope") or {}).get("country") and "country" not in c.must:
                    c.must["country"] = [str(st["scope"]["country"])]              # "remote US" = the country, for people
        return c, notes

    def _intent_text(self, brief: Brief, st: dict, direction: str) -> str:
        """The search text = the user's opening words + what the brief settled (role, specialties, place) — never the document body."""
        parts = [str(st.get("opening") or "").strip()[:200]]
        for k in ("role_family", "specialties", "skills", "mission", "must_have_done", "metro"):
            v = brief.value(k)
            if v in (None, "", []) or brief.source(k) in GAP_SOURCES:
                continue
            v = ", ".join(str(x) for x in v[:5]) if isinstance(v, list) else str(v)
            if v.lower() not in parts[0].lower():
                parts.append(v[:120])
        return ". ".join(p for p in parts if p)[:400]

    async def _read_documents(self, brief: Brief, st: dict, user, attachments_text: list[str], message: str) -> tuple[Brief, dict, list[str]]:
        """Read everything on file ONCE: an attachment, the résumé on file (seeker), the apply profile, saved JDs (hiring)."""
        V = self._v()
        notes: list[str] = []
        if not st.get("opening") and message:
            st["opening"] = message[:400]
        direction = self._direction(brief, st)
        text, kind, source = "", "", ""
        att = [a for a in attachments_text if str(a or "").strip()]
        if att:
            text, kind, source = "\n\n".join(att)[:DOC_CAP], ("jd" if direction == "candidate" else "resume"), "attachment"
        elif direction == "candidate" and self.briefs_fn is not None and user:
            try:
                jds = await self.briefs_fn(user)
            except Exception:   # noqa: BLE001
                jds = []
            st["saved_jds"] = [{"id": b.get("id"), "title": b.get("title")} for b in (jds or [])][:6]
            if len(jds or []) == 1:
                text, kind, source = str(jds[0].get("text") or "")[:DOC_CAP], "jd", "saved"
        elif direction != "candidate" and self.profile_fn is not None and user:
            try:
                prof = await self.profile_fn(user)
            except Exception:   # noqa: BLE001
                prof = None
            rt = str((prof or {}).get("_resume_text") or "")
            if len(rt) >= 200:
                text, kind, source = rt[:DOC_CAP], "resume", "on_file"
            if self.stored_fn is not None:
                try:
                    stored = await self.stored_fn(user)
                except Exception:   # noqa: BLE001
                    stored = None
                sp = ((stored or {}).get("profile") or {})
                # artifact-only facts land directly; anything that reaches the contract (the city → a metro token) goes through the reader
                for fld, keys in (("authorization", ("work_authorization", "authorization")), ("timing", ("earliest_start_date",))):
                    for k in keys:
                        if sp.get(k) and not brief.known(fld):
                            brief = brief.with_field(fld, str(sp[k])[:80], "stored", note=f"apply profile: {k}"); break
                geo = next((str(sp[k]) for k in ("city", "metro", "location") if sp.get(k)), "")
                if geo:
                    text = (text + "\n\nSTORED PROFILE — location: " + geo[:80]).strip()
                    if not kind:
                        kind, source = "profile", "stored"
        if not text and direction is None:
            return brief, st, notes                       # nothing to read until the side is known
        st["docs_read"] = True
        if not text:
            st["artifact"] = {"kind": ("jd" if direction == "candidate" else "resume"), "status": "missing"}
            return brief, st, notes
        st["artifact"] = {"kind": kind, "status": "present", "source": source, "chars": len(text)}
        st["artifact_text"] = text[:DOC_CAP]
        try:
            read = await self._model(V.document_reader_prompt(kind, direction or ("candidate" if kind == "jd" else "job")), text[:DOC_CAP])
        except Exception as e:   # noqa: BLE001
            notes.append(f"document read failed: {str(e)[:80]}"); return brief, st, notes
        # EVIDENCE GATE (spec §4; Codex): a document field lives only if its span is really in the text — the reader once
        # produced 26 "document" facts (comp, work mode, risk appetite …) from a five-line résumé that stated none of them
        hay = self._squash(text)
        dropped = []
        allowed_doc = set(V.DOCUMENT_FIELDS.get(kind, ())) | {"career_arc"}
        for fld, raw in ((read or {}).get("fields") or {}).items():
            if fld not in V.FIELD_KEYS or not isinstance(raw, dict) or raw.get("value") in (None, "", []):
                continue
            if fld not in allowed_doc:
                dropped.append(f"{fld} (a {kind} cannot state it)"); continue
            if brief.known(fld):
                continue
            span = str(raw.get("span") or "")[:240]
            if fld == "career_arc":
                brief = brief.with_field(fld, raw.get("value"), "inferred", span=span); continue
            if not span or not self._span_in(span, hay):
                dropped.append(fld); continue
            brief = brief.with_field(fld, raw.get("value"), "document", span=span)
        if dropped:
            notes.append("reader claims without a span in the text dropped: " + ", ".join(dropped[:8]))
        return brief, st, notes

    @classmethod
    def _span_in(cls, span: str, hay: str) -> bool:
        """The span is in the text: verbatim, or (a reader re-punctuates) at least 70 % of its words in order-free presence when it
        has three words or more."""
        sp = cls._squash(span)
        if not sp:
            return False
        if sp in hay:
            return True
        words = [w for w in sp.replace("(", " ").replace(")", " ").replace(",", " ").replace(".", " ").split() if len(w) > 2]
        if len(words) < 3:
            return False
        hit = sum(1 for w in words if w in hay)
        return hit / len(words) >= 0.7

    @staticmethod
    def _squash(t: str) -> str:
        return " ".join(str(t or "").lower().replace("’", "'").replace("–", "-").replace("—", "-").split())

    async def _leverage(self, contract: Contract, kind: str, brief: Brief, direction: str | None) -> tuple[list[dict], int | None, dict]:
        lev, pool, market = [], None, {}
        if direction is None:
            return lev, pool, market
        try:
            counts = await asyncio.wait_for(self.counts_fn(kind, dict(contract.must)), timeout=8.0)
            pool = pool_from_counts(counts, self.schema, kind)
            if isinstance(counts.get("metro"), dict):
                st_tokens = [v for v, _ in sorted(((v, int(n)) for v, n in counts["metro"].items() if v != "unknown"), key=lambda kv: -kv[1])[:16]]
                if st_tokens:
                    self._last_metro_tokens = st_tokens
            constrained = set(contract.must) | set(contract.prefer) | ({contract.center["key"]} if contract.center else set())
            lev = leverage(counts, self.schema, exclude=constrained | {"company", "country", "state", "posted"})
        except Exception:   # noqa: BLE001
            pass
        try:
            fld = brief.value("field") if brief.known("field") or brief.source("field") == "inferred" else None
            if fld:
                jc = await asyncio.wait_for(self.counts_fn("job", {"field": [str(fld)]}), timeout=6.0)
                market = {k: dict(sorted(((v, n) for v, n in (jc.get(k) or {}).items() if v != "unknown"), key=lambda kv: -kv[1])[:4]) for k in ("comp", "work_mode", "level", "work_type") if jc.get(k)}
        except Exception:   # noqa: BLE001
            market = {}
        return lev, pool, market

    async def _preview(self, contract: Contract, kind: str) -> dict:
        """The search as it stands: pool + the top results' role lines — what a consultant looks at before asking anything."""
        if self.evaluate_fn is None:
            return {}
        try:
            cd = contract.to_dict(); cd["limit"] = 10
            out = await asyncio.wait_for(self.evaluate_fn(cd, depth={"counts": False}), timeout=12.0)
        except Exception:   # noqa: BLE001
            return {}
        rows = list(out.get("rows") or [])[:8]
        top = []
        if kind == "person" and self.lines_fn is not None and rows:
            try:
                lines = await asyncio.wait_for(self.lines_fn([str(r.get("id") or r.get("entity_id") or "") for r in rows]), timeout=5.0)
            except Exception:   # noqa: BLE001
                lines = {}
            for r in rows:
                ln = str(lines.get(str(r.get("id") or r.get("entity_id") or "")) or "").split(" — ")[0].strip()
                f = r.get("facets") or {}
                top.append(ln or " / ".join(str((f.get(k) or ["?"])[0]) for k in ("role_family", "level", "field")))
        else:
            for r in rows:
                f = r.get("facets") or {}
                top.append(f"{r.get('title') or (f.get('role_family') or ['?'])[0]} ({(f.get('level') or ['level ?'])[0]}, {(f.get('metro') or ['place ?'])[0]})")
        cov = out.get("coverage") or {}
        return {"pool": cov.get("pool"), "best_match": cov.get("best_match"), "weak": cov.get("weak"), "top": top}

    async def _plan(self, brief: Brief, st: dict, direction: str | None, message: str, pend: dict | None, lev: list, pool, market: dict,
                    preview: dict | None = None):
        V = self._v()
        d = direction or "job"
        lines = []
        for f in V.fields_for(d):
            fl = brief.get(f.key)
            if fl and fl.value not in (None, "", []):
                lines.append(f"- {f.key} = {fl.value!r} [{fl.source}]" + (f" (from: “{fl.span[:80]}”)" if fl.span else ""))
            elif fl and fl.source == "skipped":
                lines.append(f"- {f.key}: skipped by the user — never ask again")
        asked = [q for q in (st.get("asked_fields") or [])]
        pv = preview or {}
        user = ("DIRECTION: " + d + "\n"
                + "USER'S WORDS (opening): " + str(st.get("opening") or "")[:400] + "\n"
                + "BRIEF SO FAR:\n" + ("\n".join(lines) or "(nothing yet)") + "\n"
                + f"ARTIFACT: {st.get('artifact')}\n" + (f"SAVED JDS: {st.get('saved_jds')}\n" if st.get("saved_jds") else "")
                + "PREVIEW of the current search: " + (f"pool {pv.get('pool')}" + (f", best match {pv.get('best_match')} %" if pv.get("best_match") is not None else "")
                                                       + ("; top: " + " · ".join(pv.get("top") or []) if pv.get("top") else "; no rows") if pv else f"pool {pool if pool is not None else 'unknown'}") + "\n"
                + "LEVERAGE (what splits the pool; counts): " + ("; ".join(f"{x['key']} → " + ", ".join(f"{v} {n}" for v, n in x["values"]) for x in lev) or "none measured") + "\n"
                + "MARKET (open postings in this domain): " + ("; ".join(f"{k}: " + ", ".join(f"{V.option_label(k, v)} {n}" for v, n in d2.items()) for k, d2 in market.items()) or "n/a") + "\n"
                + "METRO TOKENS: " + ", ".join(st.get("metro_tokens") or ["new_york", "bay_area", "seattle", "los_angeles", "boston", "chicago", "austin", "london", "bangalore"]) + "\n"
                + f"QUESTIONS ASKED SO FAR: {len(asked)} of {V.MAX_QUESTIONS} (fields: {', '.join(asked) or 'none'})\n"
                + "TRANSCRIPT (latest last):\n" + "\n".join(f"{t['role']}: {t['text']}" for t in (st.get("transcript") or [])[-TRANSCRIPT_KEEP:])
                + (f"\nPENDING QUESTION the user typed an answer to: {pend.get('text')} (field {pend.get('field')})" if (pend and message) else "")
                + (f"\nLATEST USER MESSAGE: {message}" if message else "\n(no new message — decide from the brief and the preview)"))
        try:
            raw = await asyncio.to_thread(self.planner, V.planner_prompt(d), user[:12000])
        except Exception as e:   # noqa: BLE001
            raw = {"move": "ready", "say": "I'll search with what I have.", "_error": str(e)[:80]}
        move = parse_move(self._adapt(raw), brief=brief, schema=self.schema, allowed_fields=V.FIELD_KEYS)
        move.notes = [n for n in move.notes] + ([f"planner failed: {raw['_error']}"] if raw.get("_error") else [])
        if move.question and not move.question.field:
            # a question is always ABOUT a field: the planner's top high-impact gap, else the first open required one
            gaps = [g.get("field") for g in (raw.get("gaps") or []) if isinstance(g, dict) and g.get("impact") == "high" and g.get("field") in V.FIELD_KEYS]
            open_req = readiness(brief, self._required(brief, direction), accept_inferred=True) if direction else []
            cand = (gaps[0] if gaps else None) or (open_req[0] if open_req else None)
            if cand:
                move.question.field = cand; move.notes.append(f"question resolved to the field {cand!r}")
        if isinstance(raw.get("preview_verdict"), str):
            move.notes.append("preview: " + raw["preview_verdict"][:100])
        if isinstance(raw.get("gaps"), list):
            move.notes.append("gaps: " + ", ".join(f"{g.get('field')}={g.get('impact')}" for g in raw["gaps"] if isinstance(g, dict))[:160])
        return move

    @staticmethod
    def _adapt(raw: dict) -> dict:
        """The search-first output → the kernel's move shape: understanding → brief_delta; search → contract_delta; ask → fork."""
        raw = raw if isinstance(raw, dict) else {}
        mv = str(raw.get("move") or "").lower()
        move = {"ask": "fork", "ready": "ready", "draft": "draft", "restart": "restart", "split": "split"}.get(mv, "infer")
        q = raw.get("question") if move == "fork" else None
        search = raw.get("search") if isinstance(raw.get("search"), dict) else {}
        cd = {k: v for k, v in search.items() if k in ("must", "prefer", "avoid", "center")}
        return {"move": move, "say": raw.get("say") or "", "brief_delta": raw.get("understanding") if isinstance(raw.get("understanding"), dict) else {},
                "question": q, "contract_delta": cd, "ready": move == "ready", "search_text": search.get("text")}

    async def _probe_options(self, kind: str, brief: Brief, st: dict, direction: str, options: list[dict]) -> dict:
        if self.slice_fn is None:
            return {}
        base, _ = self._contract(brief, st, direction)

        async def one(o):
            must = dict(base.must)
            for key, vals in ((o.get("effect") or {}).get("must") or {}).items():
                must[key] = sorted(set(list(must.get(key) or []) + [str(v) for v in vals]))
            if must == base.must:
                return o["label"], None
            try:
                return o["label"], await self.slice_fn(kind, must)
            except Exception:   # noqa: BLE001
                return o["label"], None
        return dict(await asyncio.gather(*[one(o) for o in options]))

    async def _draft(self, brief: Brief, st: dict, say: str) -> tuple[Brief, dict, str]:
        V = self._v()
        ctx = {}
        for k in V.JD_INTERVIEW + ("role_family", "field", "comp", "metro", "work_mode", "company_type"):
            v = brief.value(k)
            if v not in (None, "", []) and brief.source(k) not in GAP_SOURCES:
                ctx[V.FIELDS_BY_KEY[k].label] = ", ".join(map(str, v)) if isinstance(v, list) else str(v)
        role_text = self._intent_text(brief, st, "candidate") or str(st.get("opening") or "")
        try:
            draft = await self.draft_fn(role_text, ctx)
        except Exception as e:   # noqa: BLE001
            return brief, st, f"I couldn't build the draft just now ({str(e)[:60]}); tell me the must-haves in your words and I'll try again."
        st["jd"] = draft
        text = str(draft.get("text") or "")
        st["artifact"] = {"kind": "jd", "status": "present", "source": "drafted", "chars": len(text)}
        st["artifact_text"] = text[:DOC_CAP]
        peers = len(draft.get("peers") or [])
        return brief, st, (say or "Here is the JD from what you told me" + (f" and the centre of {peers} peer postings" if peers else "") + ". Edit anything, or accept it and I'll search.")

    async def _ready(self, brief: Brief, st: dict, direction: str, *, say: str) -> dict:
        V = self._v()
        kind = V.SEARCH_KIND.get(direction, "job")
        c, cnotes = self._contract(brief, st, direction)
        mapping = V.contract_mapping(direction)
        user_keys = sorted({mapping[k][0] for k in mapping if brief.source(k) in ("stated", "asked")} | {k for k in ((st.get("overlay") or {}).get("must") or {})})
        notes: list = []
        if self.index_aware_fn is not None:
            try:
                c, notes = await self.index_aware_fn(c, kind=kind, user_keys=set(user_keys), place_or_mode=bool(brief.value("work_mode") and brief.value("metro")))
            except Exception:   # noqa: BLE001
                notes = []
        counts, pool = {}, None
        try:
            counts = await self.counts_fn(kind, dict(c.must)); pool = pool_from_counts(counts, self.schema, kind)
        except Exception:   # noqa: BLE001
            pass
        handoff = {**c.to_dict(), "user_keys": user_keys, "notes": notes, "place_or_mode": bool(brief.value("work_mode") and brief.value("metro"))}
        assumptions = [f"{V.FIELDS_BY_KEY[k].label}: {brief.value(k)} (inferred)" for k in V.FIELD_KEYS if brief.source(k) == "inferred" and brief.value(k) not in (None, "", []) and k in mapping]
        return {"direction": direction, "search_kind": kind, "understood": say, "contract": handoff, "counts": counts, "pool": pool, "notes": notes,
                "assumptions": assumptions, "artifact": dict(st.get("artifact") or {}), "brief": self._brief_view(brief, direction), "jd": st.get("jd")}

    def _brief_view(self, brief: Brief, direction: str | None) -> list[dict]:
        V = self._v()
        out = []
        for f in V.fields_for(direction or "job"):
            fl = brief.get(f.key)
            if not fl or fl.value in (None, "", []) and fl.source != "skipped":
                continue
            out.append({"key": f.key, "label": f.label, "value": fl.value, "source": fl.source, "source_label": V.DOCUMENT_LABEL.get(fl.source, fl.source), "span": fl.span[:160]})
        return out

    async def _record(self, st: dict, brief: Brief, direction: str | None, *, stage: str) -> None:
        if self.record_fn is None:
            return
        try:
            await self.record_fn({"id": st.get("record_id"), "direction": direction, "stage": stage, "brief": brief.to_dict(), "overlay": st.get("overlay") or {},
                                  "transcript": (st.get("transcript") or [])[-TRANSCRIPT_KEEP:], "artifact": st.get("artifact"), "calls": st.get("calls"), "forks": st.get("forks")})
        except Exception:   # noqa: BLE001
            pass

    def _pack(self, st: dict, brief: Brief, question: dict | None, *, say: str, stage: str, notes: list, direction: str | None = None, ready: dict | None = None, pool=None) -> dict:
        st = dict(st); st["brief"] = brief.to_dict(); st["transcript"] = (st.get("transcript") or [])[-TRANSCRIPT_KEEP:]
        return {"stage": stage, "say": say, "question": question, "ready": ready, "direction": direction, "direction_source": brief.source("direction"), "pool": pool,
                "brief": self._brief_view(brief, direction), "artifact": dict(st.get("artifact") or {}), "jd": st.get("jd"),
                "notes": [n for n in notes if n][:12], "state": st}


# ---------------------------------------------------------------------------------------------------------------
# the write-behind record (spec §8): history / start-over recovery / resume by id — never a dependency of a turn
# ---------------------------------------------------------------------------------------------------------------
_RECORD_DDL = """
CREATE TABLE IF NOT EXISTS roster_intake_record (
  id TEXT PRIMARY KEY,
  user_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  direction TEXT,
  stage TEXT,
  record JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rir_user ON roster_intake_record (user_id, updated_at DESC);
"""


def record_writer(get_pool, user_id: str | None):
    import json as _json
    state = {"ddl": False}

    async def write(rec: dict) -> None:
        pool = await get_pool()
        if pool is None or not rec.get("id"):
            return
        async with pool.acquire() as conn:
            if not state["ddl"]:
                await conn.execute(_RECORD_DDL); state["ddl"] = True
            await conn.execute("""INSERT INTO roster_intake_record (id, user_id, direction, stage, record) VALUES ($1, $2, $3, $4, $5::jsonb)
                                  ON CONFLICT (id) DO UPDATE SET user_id = EXCLUDED.user_id, direction = EXCLUDED.direction, stage = EXCLUDED.stage,
                                  record = EXCLUDED.record, updated_at = now()""",
                               str(rec["id"]), (str(user_id) if user_id else None), rec.get("direction"), rec.get("stage"), _json.dumps(rec))
    return write
