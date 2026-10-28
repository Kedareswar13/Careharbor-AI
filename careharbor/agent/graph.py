"""CareHarbor AI LangGraph: safety gate -> router -> agents -> output guard.

Graph (one invocation = one patient turn; the SQLite checkpointer keeps each thread's state):

    START -> safety_gate --red flag--> emergency ----------------------------> finalize -> END
                  |
                  v
               router --> identity --(verified, resume)--> booking | prescription
                  |-----> triage | booking | prescription | faq | handoff | smalltalk | blocked
    booking --(slot/appointment chosen)--> confirm [interrupt() -> UI card] --> commit
    every agent --> output_guard --> finalize -> END

LLM calls happen only in: router fallback (unclear messages), the triage safety classifier and
guidance, and FAQ answers. Identity, booking, prescriptions and handoff are deterministic and
template-based, so a reply can only state what a tool returned from the database.
"""

from __future__ import annotations

import operator
import re
import time
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from careharbor.agent import parsing as P
from careharbor.agent import triage as T
from careharbor.cache import faq_cache
from careharbor.config import get_settings
from careharbor.db import VERIFIED_SQL, audit, connect, verified_cutoff
from careharbor.guardrails import SAFE_FALLBACK, check_output, emergency_reply, red_flags
from careharbor.llm import BaseLLM, get_llm
from careharbor.rag.advanced import attribute, contextualize, parent_context
from careharbor.rag.index import get_index
from careharbor.rag.navigator import retrieve
from careharbor.security import SCOPE_REPLY, TEMPLATES, log_retrieval, principal_for_chat, sanitise_retrieved, validate_query
from careharbor.tools.client import ToolClient
from careharbor.tools.service import SPECIALTY_ALIASES

INTENTS = ["triage", "booking", "prescription", "faq", "handoff", "smalltalk", "out_of_scope"]
NOT_FOUND_REPLY = "I don't have that information in Sunrise Family Clinic's documents, so I won't guess. I can connect you with our front desk if you like."

# ----------------------------------------------------------------- router rules
RX = {
    "handoff": re.compile(r"(?i)\b(talk|speak|connect|chat)\W+(to|with)\W+(a\W+|an\W+|the\W+|some\W+)?(person|human|someone|somebody|staff|agent|receptionist|real|nurse|pharmacist|front desk)"
                          r"|\bhuman\b|\bcall me( back)?\b|\bcomplain\w*|\breal person\b|\bmanager\b"),
    "prescription": re.compile(r"(?i)\b(prescriptions?|prescribed|medicines?|medications?|tablets?|capsules?|antibiotics?|doses?|dosage|syrups?|inhalers?|pills?|drugs?|dawai|dawa|goli"
                               r"|ointment|cream|sachet|amoxicillin|paracetamol|pantoprazole|metformin|amlodipine|lisinopril|atorvastatin|simvastatin|ibuprofen|naproxen|cetirizine)\b"),
    "booking": re.compile(r"(?i)\b(book\w*|appointments?|appt|reschedul\w*|cancel\w*|slots?|see\W+(dr|doctor|the doctor)|visit (the )?(dr|doctor)|consult\w*|availab\w*|move my|postpone|prepone)\b"
                          r"|\bdr\.?\s+[a-z]+"),
    "faq": re.compile(r"(?i)\b(timings?|hours|open|opening|close[sd]?|sunday|holidays?|insurance|insurer|cashless|tpa|star health|fees?|cost|charges?|price|pay|payment|upi|card|how much|consultation|free|discount|follow.?up (visit|fee)"
                      r"|parking|visitors?|visiting|companion|attendant|lab|reports?|blood test|fasting|privacy|my data|departments?|which doctor|address|location|where is|wheelchair|refund|policy|vaccination)\b"),
    "triage": re.compile(r"(?i)\b(fever|temperature|pain|ache|aching|headache|migraine|cough|cold|sore|throat|vomit\w*|nausea|diarrh\w*|loose motions?|rash|itch\w*|sneez\w*|allerg\w*|burn\w*|cut|wound"
                         r"|sprain\w*|swell\w*|swollen|bleed\w*|dizz\w*|tired|fatigue|sleep|insomnia|acidity|heartburn|reflux|gas|bp|blood pressure|sugar|diabet\w*|injur\w*|hurt\w*|sick|unwell|ill|symptoms?"
                         r"|bukhar|dard|khansi|zukam|ulti|dast|khujli|chot|neend|jalan)\b|\bnot feeling (well|good)\b"),
    "smalltalk": re.compile(r"(?i)^\W*(hi|hello|hey|hii+|namaste|good (morning|afternoon|evening)|thanks?|thank you|thx|ok|okay|bye|who are you|what can you do|help)\W*$"),
}
ACTION_WORDS = re.compile(r"(?i)\b(book|reschedul\w*|cancel|change|move|postpone|prepone)\b")
FORGET_RX = re.compile(r"(?i)\b(forget|erase|delete|remove|wipe)\b\W+(\w+\W+)?(me|my (data|details|preferences|memory|history))\b")
INFO_QUESTION = re.compile(r"(?i)\b(how much|how many|fees?|cost|price|charges?|timings?|hours|open|insurance|cashless|policy|refund|pay|payment|free|discount|allowed|companions?|bring)\b")
POLICY_QUESTION = re.compile(r"(?i)\bhow (late|early|long before)\b|\bin advance\b|\bwithout (a |any )?(charge|fee|penalty|problem)\b|\bcancellation policy\b|\bis there a (fee|charge)\b|\bcan i bring\b")
ESCALATE_RX = re.compile(
    r"(?i)\b(stop|stopping|quit|discontinu\w*|double|extra (dose|tablet)|take more|more (tablets?|doses?|pills?|than)|increase|decrease|reduce|less (tablets?|dose)|skip\w*|missed|forgot|forget"
    r"|change (the |my )?(dose|medicine|tablet)|switch|instead|another (medicine|tablet|drug|antibiotic)|other medicines?|alcohol|beer|wine|drink alcohol"
    r"|side.?effects?|reaction|allergic reaction|am allergic|allergic to|pregnan\w*|breastfeed\w*|together|combine|safe to take|can i (take|have|use|give)|should i (take|have|use|give)|is it ok to)\b"
)
FOOD_QUESTION = re.compile(r"(?i)\b(with|after|before|without|on an empty)\W+(\w+\W+)?(food|meals?|breakfast|lunch|dinner|stomach|milk)\b|\bwhat time\b|\bwhen\b")
SOFT_ESCALATE = re.compile(r"(?i)^(can i (take|have|use|give)|should i (take|have|use|give)|is it ok to)$")
SEVERE_REACTION = re.compile(r"(?i)\b(reaction|side.?effects?|rash|swelling|itch\w*|vomit\w*|allerg\w*)\b")
DRUG_CLASSES = {
    "antibiotic": {"amoxicillin", "amoxicillin + clavulanic acid", "azithromycin", "penicillin v", "cefuroxime", "cephalexin", "ciprofloxacin", "doxycycline", "nitrofurantoin"},
    "pain": {"paracetamol", "paracetamol chewable", "ibuprofen", "naproxen"},
    "acid": {"pantoprazole"},
    "bp": {"amlodipine", "lisinopril", "losartan", "hydrochlorothiazide", "carvedilol", "metoprolol succinate (extended release)", "furosemide", "telmisartan"},
    "sugar": {"metformin (extended release)"},
    "cholesterol": {"atorvastatin", "simvastatin", "rosuvastatin"},
    "inhaler": {"salbutamol inhaler", "fluticasone inhaler", "fluticasone + salmeterol inhaler"},
    "allergy": {"cetirizine", "loratadine", "fexofenadine", "montelukast", "levocetirizine"},
}
CLASS_WORDS = {
    "antibiotic": r"antibiotic", "pain": r"pain\s*killer|painkiller|pain|fever|paracetamol|crocin|dolo", "acid": r"acid|acidity|gas|stomach|gastric",
    "bp": r"\bbp\b|blood pressure|hypertension", "sugar": r"sugar|diabet", "cholesterol": r"cholesterol|statin", "inhaler": r"inhaler|puff|asthma|wheez",
    "allergy": r"allerg|sneez|antihistamine|hay fever",
}


def _merge_list(a: list, b: list) -> list:
    """Append reducer with a per-turn reset: the runtime sends ["__reset__"] at the start of each turn."""
    if b and b[0] == "__reset__":
        return list(b[1:])
    return (a or []) + (b or [])


class ChatState(TypedDict, total=False):
    session_id: str
    text: str
    model: str
    messages: Annotated[list[dict], operator.add]
    pending: dict | None
    intent: str
    route_source: str
    reply: str
    reply_kind: str            # llm | template | emergency
    grounding: str
    card: dict | None
    citations: list[dict]
    stages: Annotated[list[dict], _merge_list]
    guardrails: Annotated[list[dict], _merge_list]
    llm_usage: Annotated[list[dict], _merge_list]
    prefix: str
    resume: dict | None
    confirm: dict | None
    decision: str
    memory_key: str            # opaque long-term memory key for a verified patient (never the patient id)
    last_faq: str              # previous FAQ question, for conversational query rewriting
    evidence: list[dict]       # sentence-level attribution of the last grounded answer


class CareHarborGraph:
    def __init__(self, tools: ToolClient, *, llm_factory: Callable[[str | None], BaseLLM] = get_llm, clock: Callable[[], datetime] | None = None, store: Any = None, db_path: Any = None):
        from careharbor.db import clinic_now

        self.tools = tools
        self.llm_factory = llm_factory
        self.clock = clock or clinic_now
        self.store = store
        self.db_path = db_path

    # ------------------------------------------------------------- utilities
    def _llm(self, state: ChatState, fast: bool = False) -> BaseLLM:
        """Answers use the patient's selected model; short JSON classifications use the faster model."""
        from careharbor.config import get_settings

        if fast and get_settings().llm_fast_model:
            return self.llm_factory(get_settings().llm_fast_model)
        return self.llm_factory(state.get("model") or None)

    def _stage(self, key: str, label: str, t0: float, **extra: Any) -> list[dict]:
        return [{"key": key, "label": label, "ms": round((time.perf_counter() - t0) * 1000, 1), **extra}]

    def _audit(self, session_id: str, actor: str, event: str, **detail: Any) -> None:
        conn = connect(self.db_path)
        try:
            audit(conn, actor, event, session_id, **detail)
        finally:
            conn.close()

    async def _call(self, agent: str, tool: str, args: dict | None, state: ChatState) -> dict:
        return await self.tools.call(agent, tool, args or {}, session_id=state["session_id"])

    async def _verified(self, state: ChatState) -> bool:
        """Is this chat session verified? Reads only the session status - no PHI."""
        conn = connect(self.db_path)
        try:
            row = conn.execute(f"SELECT 1 FROM identity_sessions WHERE session_id=? AND {VERIFIED_SQL}", (state["session_id"], verified_cutoff())).fetchone()
            return row is not None
        finally:
            conn.close()

    async def _remember(self, state: ChatState, key: str, value: Any) -> None:
        """Long-term memory. Verified patients: namespace = opaque memory key (survives new chats, never the patient id).
        Unverified chats: namespace = the session."""
        if self.store is None:
            return
        ns = ("patients", state["memory_key"]) if state.get("memory_key") else ("sessions", state["session_id"])
        item = await self.store.aget(ns, "prefs")
        prefs = dict(item.value) if item else {}
        prefs[key] = value
        await self.store.aput(ns, "prefs", prefs)

    async def _recall(self, state: ChatState, key: str) -> Any:
        if self.store is None:
            return None
        for ns in ([("patients", state["memory_key"])] if state.get("memory_key") else []) + [("sessions", state["session_id"])]:
            item = await self.store.aget(ns, "prefs")
            if item and key in item.value:
                return item.value[key]
        return None

    # ------------------------------------------------------------- 1 safety
    async def safety_gate(self, state: ChatState) -> dict:
        t0 = time.perf_counter()
        text = state["text"]
        pending = state.get("pending") or {}
        combined = text if pending.get("flow") != "triage" else f"{pending.get('symptom', '')} . {text}"
        rf = red_flags(combined)
        out: dict = {"stages": self._stage("safety", "Checking safety", t0, red_flag=rf.hit)}
        if rf.hit:
            out.update(intent="emergency", route_source="red_flag_rules", guardrails=[{"type": "red_flag", "categories": rf.categories, "matches": rf.matches}])
            self._audit(state["session_id"], "guardrail", "red_flag", categories=rf.categories)
        return out

    def after_safety(self, state: ChatState) -> str:
        return "emergency" if state.get("intent") == "emergency" else "router"

    async def emergency(self, state: ChatState) -> dict:
        t0 = time.perf_counter()
        rf = red_flags(f"{(state.get('pending') or {}).get('symptom', '')} . {state['text']}")
        cats = rf.categories or ["llm_safety_classifier"]
        summary = f"EMERGENCY red flag ({', '.join(cats)}). Patient was told to call 112/108.\nMessage: {state['text'][:160]}"
        ticket = await self._call("safety", "create_handoff", {"summary": summary, "priority": "urgent", "kind": "emergency"}, state)
        return {
            "reply": emergency_reply(rf) if rf.hit else emergency_reply(rf),
            "reply_kind": "emergency",
            "card": {"type": "emergency", "call": ["112", "108"], "categories": cats, "ticket_id": ticket.get("ticket_id"), "crisis_line": "14416" if rf.crisis else None},
            "pending": None,
            "stages": self._stage("alert", "Alerting clinic staff", t0),
        }

    # ------------------------------------------------------------- 2 router
    async def router(self, state: ChatState) -> dict:
        t0 = time.perf_counter()
        text = state["text"]
        pending = state.get("pending") or {}
        verdict = validate_query(text)  # Layer 1: nothing below (retriever, LLM, tools) sees a rejected query
        if not verdict.allowed and not (pending.get("flow") == "identity" and verdict.reason == "identifier_probe"):
            self._audit(state["session_id"], "guardrail", "query_blocked", reason=verdict.reason, matches=verdict.matches[:3])
            intent = "out_of_scope" if verdict.reason == "out_of_scope" else "blocked"
            return {"intent": intent, "route_source": f"query_guard:{verdict.reason}", "guardrails": [{"type": "query_guard", "reason": verdict.reason, "matches": verdict.matches[:3]}],
                    "pending": None, "stages": self._stage("understand", "Understanding your message", t0)}
        hits = {k for k, rx in RX.items() if rx.search(re.sub(r"(?i)\bgeneral medicine\b", "general-department", text))}
        flow = pending.get("flow")
        if FORGET_RX.search(text):
            return {"intent": "forget", "route_source": "rules", "pending": None, "stages": self._stage("understand", "Understanding your message", t0)}
        # A strong new request overrides an unfinished flow; otherwise continue the flow.
        if "handoff" in hits:
            intent, source = "handoff", "rules"
        elif flow == "identity" and not (hits & {"faq"} and not (P.find_phone(text) or P.find_otp(text))):
            intent, source = "identity", "pending"
        elif flow == "booking" and pending.get("step") in {"choose", "choose_appt", "need_doctor"} and not (hits & {"prescription", "faq"} and not ACTION_WORDS.search(text)):
            intent, source = "booking", "pending"
        elif flow == "triage" and pending.get("step") == "followup" and not (hits & {"booking", "prescription", "faq"}):
            intent, source = "triage", "pending"
        elif flow == "triage" and pending.get("step") == "offer_booking" and P.yes_no(text) == "yes":
            intent, source = "booking", "pending"
        elif flow == "triage" and pending.get("step") == "offer_booking" and P.yes_no(text) == "no":
            intent, source = "smalltalk", "pending"
        elif flow == "prescription" and not (hits & {"booking", "faq", "triage"} and not ESCALATE_RX.search(text)):
            intent, source = "prescription", "pending"
        elif POLICY_QUESTION.search(text) or ("faq" in hits and not ACTION_WORDS.search(text) and INFO_QUESTION.search(text)):
            intent, source = "faq", "rules"
        elif "booking" in hits and (ACTION_WORDS.search(text) or "prescription" not in hits):
            intent, source = "booking", "rules"
        elif "prescription" in hits:
            intent, source = "prescription", "rules"
        elif "faq" in hits and "triage" not in hits:
            intent, source = "faq", "rules"
        elif "triage" in hits:
            intent, source = "triage", "rules"
        elif "smalltalk" in hits:
            intent, source = "smalltalk", "rules"
        else:
            intent, source = await self._llm_route(state)
        out = {"intent": intent, "route_source": source, "stages": self._stage("understand", "Understanding your message", t0, intent=intent, source=source)}
        if flow and intent not in {flow, "identity"} and not (flow == "triage" and intent == "booking"):
            out["pending"] = None
        if flow == "prescription" and intent == "prescription":
            out["pending"] = None  # follow-up consumed; the prescription agent sets it again after answering
        return out

    async def _llm_route(self, state: ChatState) -> tuple[str, str]:
        system, user = TEMPLATES["router"].render(question=state["text"])
        reply = await self._llm(state, fast=True).achat(system, user, task="router", json_mode=True, max_tokens=30)
        data = reply.json()
        intent = data.get("intent") if data.get("intent") in INTENTS else "out_of_scope"
        if data.get("emergency") is True:
            self._audit(state["session_id"], "guardrail", "llm_safety_emergency", text_len=len(state["text"]))
            return "emergency", "llm_safety"
        return intent, "llm"

    def after_router(self, state: ChatState) -> str:
        return state["intent"]

    # ------------------------------------------------------------- 3 identity
    async def identity(self, state: ChatState) -> dict:
        t0 = time.perf_counter()
        text = state["text"]
        pending = state.get("pending") or {"flow": "identity", "step": "details", "resume": None}
        resume = pending.get("resume")
        if P.yes_no(text) == "no" and not P.find_otp(text):
            return {"reply": "No problem, I've stopped the verification. Is there anything else I can help with?", "reply_kind": "template", "pending": None,
                    "stages": self._stage("identity", "Verifying identity", t0)}
        if pending.get("step") == "otp":
            code = P.find_otp(text)
            if not code:
                return {"reply": "Please type the 6-digit code I sent (or say 'resend').", "reply_kind": "template", "pending": pending, "stages": self._stage("identity", "Verifying identity", t0)}
            r = await self._call("identity", "confirm_otp", {"code": code}, state)
            if not r.get("ok"):
                if r.get("error") in {"otp_expired", "otp_locked", "no_pending_otp"}:
                    pending = {**pending, "step": "details"}
                return {"reply": r["message"] + (" Please share your mobile number and date of birth again." if pending["step"] == "details" else ""), "reply_kind": "template",
                        "pending": pending, "stages": self._stage("identity", "Verifying identity", t0)}
            prefix = f"Thanks, {r.get('first_name', '')} - you're verified. "
            mem = {"memory_key": r.get("memory_key", "")}
            if self.store is not None and mem["memory_key"]:
                item = await self.store.aget(("patients", mem["memory_key"]), "prefs")
                if item and item.value.get("preferred_doctor_name"):
                    prefix += f"Welcome back - last time you booked with {item.value['preferred_doctor_name']}. "
            if resume:
                return {**mem, "prefix": prefix, "intent": resume["intent"], "text": resume["text"], "pending": resume.get("pending"), "resume": resume,
                        "stages": self._stage("identity", "Verifying identity", t0)}
            return {**mem, "reply": prefix + "How can I help?", "reply_kind": "template", "pending": None, "stages": self._stage("identity", "Verifying identity", t0)}
        phone, dob = P.find_phone(text), P.find_dob(text)
        phone = phone or pending.get("phone")
        dob = dob or pending.get("dob")
        if not (phone and dob):
            ask = "your registered mobile number and date of birth (DD/MM/YYYY)" if not (phone or dob) else ("your date of birth (DD/MM/YYYY)" if phone else "your registered mobile number")
            return {"reply": f"To protect your records, I need to verify you first. Please share {ask}.", "reply_kind": "template",
                    "pending": {**pending, "flow": "identity", "step": "details", "phone": phone, "dob": dob}, "card": {"type": "identity"},
                    "stages": self._stage("identity", "Verifying identity", t0)}
        r = await self._call("identity", "verify_identity", {"phone": phone, "dob": dob}, state)
        if not r.get("ok"):
            return {"reply": r["message"], "reply_kind": "template", "pending": {**pending, "step": "details", "phone": None, "dob": None},
                    "stages": self._stage("identity", "Verifying identity", t0)}
        if r.get("status") == "verified":
            if resume:
                return {"intent": resume["intent"], "text": resume["text"], "pending": resume.get("pending"), "resume": resume, "stages": self._stage("identity", "Verifying identity", t0)}
            return {"reply": "You're already verified. How can I help?", "reply_kind": "template", "pending": None, "stages": self._stage("identity", "Verifying identity", t0)}
        demo = f" (Demo mode: your code is {r['demo_otp']}.)" if r.get("demo_otp") else ""
        return {"reply": f"I've sent a 6-digit code to {r['sent_to']}. Please type it here.{demo}", "reply_kind": "template",
                "pending": {"flow": "identity", "step": "otp", "resume": resume}, "card": {"type": "otp", "sent_to": r["sent_to"], "demo_otp": r.get("demo_otp")},
                "stages": self._stage("identity", "Verifying identity", t0)}

    def after_identity(self, state: ChatState) -> str:
        return state["intent"] if state.get("resume") and not state.get("reply") and state.get("intent") in {"booking", "prescription"} else "output_guard"

    async def _require_identity(self, state: ChatState, intent: str, keep_pending: dict | None = None) -> dict | None:
        if await self._verified(state):
            return None
        resume = {"intent": intent, "text": state["text"], "pending": keep_pending}
        phone, dob = P.find_phone(state["text"]), P.find_dob(state["text"])
        sub = {**state, "pending": {"flow": "identity", "step": "details", "resume": resume, "phone": phone, "dob": dob}}
        return await self.identity(sub)  # type: ignore[arg-type]

    # ------------------------------------------------------------- 4 triage
    async def triage(self, state: ChatState) -> dict:
        t0 = time.perf_counter()
        text = state["text"]
        pending = state.get("pending") or {}
        if pending.get("flow") == "triage" and pending.get("step") == "followup":
            return await self._triage_guidance(state, pending, t0)
        who = principal_for_chat(state["session_id"], await self._verified(state))
        r = retrieve(text, "care", purpose="triage", llm=self._llm(state), who=who)
        log_retrieval(state["session_id"], who, text, r.hits, "triage_followup" if r.hits else "no_match", self.db_path, mode=r.mode)
        stages = self._stage("retrieve", "Reading clinic care leaflets", t0, docs=[h.doc_id for h in r.hits][:2], mode=r.mode)
        if not r.hits:
            return {"reply": "I couldn't find guidance for that in our leaflets. Would you like me to book you with a doctor, or connect you to our staff?",
                    "reply_kind": "template", "pending": None, "stages": stages}
        # Second safety layer: a short LLM classifier on the first symptom message.
        t1 = time.perf_counter()
        system, user = TEMPLATES["safety"].render(question=text)
        verdict = (await self._llm(state, fast=True).achat(system, user, task="safety", json_mode=True, max_tokens=12)).json()
        stages += self._stage("safety_llm", "Double-checking for warning signs", t1)
        if verdict.get("emergency") is True:
            self._audit(state["session_id"], "guardrail", "llm_safety_emergency")
            return {"intent": "emergency", "route_source": "llm_safety", "guardrails": [{"type": "llm_safety", "emergency": True}], "stages": stages}
        doc_id = r.hits[0].doc_id
        qs = T.followup_questions(text, r.hits)
        reply = "I'm sorry you're not feeling well. A few quick questions so I can guide you safely:\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(qs, 1))
        return {"reply": reply, "reply_kind": "template", "pending": {"flow": "triage", "step": "followup", "symptom": text, "doc_id": doc_id},
                "citations": r.citations[:1], "stages": stages, "card": {"type": "followup", "questions": qs}}

    def after_triage(self, state: ChatState) -> str:
        return "emergency" if state.get("intent") == "emergency" else "output_guard"

    async def _triage_guidance(self, state: ChatState, pending: dict, t0: float) -> dict:
        answer = state["text"]
        story = f"{pending['symptom']} . {answer}"
        yn = P.yes_no(answer)
        if yn == "yes" and not re.search(r"(?i)\b(no|none|not|nahi|nahin)\b", answer):
            # "Yes" to the emergency-signs screening question: err on the side of caution.
            self._audit(state["session_id"], "guardrail", "positive_screen")
            return {"intent": "emergency", "route_source": "screening_answer", "guardrails": [{"type": "positive_screen"}], "pending": {**pending},
                    "stages": self._stage("screen", "Checking your answers", t0)}
        who = principal_for_chat(state["session_id"], await self._verified(state))
        r = retrieve(pending["symptom"], "care", purpose="triage", who=who)
        sections = [h for h in r.hits if h.doc_id == pending["doc_id"]] or r.hits
        log_retrieval(state["session_id"], who, pending["symptom"], sections, "triage_guidance", self.db_path)
        dispo = T.disposition(story, pending["doc_id"])
        self_care, see_doctor = T.guidance_bullets(sections)
        context = sanitise_retrieved("Self-care:\n" + "\n".join(f"- {b}" for b in self_care) + "\nSee a doctor if:\n" + "\n".join(f"- {b}" for b in see_doctor))
        stages = self._stage("retrieve", "Reading clinic care leaflets", t0, disposition=dispo.level)
        t1 = time.perf_counter()
        system, user = TEMPLATES["triage"].render(context=context, question=story)
        reply = await self._llm(state).achat(system, user, task="answer", max_tokens=160)
        llm_usage = [{"task": "triage_answer", "model": reply.model, "in": reply.prompt_tokens, "out": reply.completion_tokens, "ms": reply.ms}]
        stages += self._stage("write", "Writing the answer", t1)
        body = reply.text.strip()
        check = check_output(body, kind="llm", grounding=context + " " + story)
        guard = []
        if not check.ok or len(body) < 20:
            guard = [{"type": "output_guard", "violations": check.violations, "details": check.details[:3], "action": "extractive_fallback"}]
            self._audit(state["session_id"], "guardrail", "output_blocked", violations=check.violations)
            body = "Here is what our care leaflet suggests:\n" + "\n".join(f"- {b}" for b in self_care[:4])
        dept = "Paediatrics" if T.CHILD.search(story) else T.DEPARTMENT_FOR_DOC.get(pending["doc_id"], "General Medicine")
        when = "\n\nWhen to see a doctor:\n" + "\n".join(f"- {b}" for b in see_doctor) if see_doctor else ""
        offer = f"\n\n{dispo.advice} Would you like me to book an appointment with {dept}?" if dispo.level != "self_care" else f"\n\n{dispo.advice} If you'd like, I can book you with {dept}."
        return {"reply": body + when + offer, "reply_kind": "llm_checked", "grounding": context + " " + story, "citations": r.citations,
                "evidence": attribute(body, sections),
                "pending": {"flow": "triage", "step": "offer_booking", "specialty": dept, "reason": pending["symptom"][:80], "disposition": dispo.level},
                "card": {"type": "triage", "disposition": dispo.level, "reasons": dispo.reasons, "department": dept},
                "stages": stages, "llm_usage": llm_usage, "guardrails": guard}

    # ------------------------------------------------------------- 5 booking
    async def booking(self, state: ChatState) -> dict:
        t0 = time.perf_counter()
        pending = state.get("pending") or {}
        gate = await self._require_identity(state, "booking", pending if pending.get("flow") in {"triage", "booking"} else None)
        if gate:
            return gate
        text = state["text"]
        if pending.get("flow") == "booking" and pending.get("step") == "choose":
            return await self._booking_choose(state, pending, t0)
        if pending.get("flow") == "booking" and pending.get("step") == "choose_appt":
            idx = P.parse_choice(text, len(pending["appointments"]))
            if idx is None:
                return {"reply": "Which appointment? Please reply with its number.", "reply_kind": "template", "pending": pending, "stages": self._stage("records", "Looking up your appointments", t0)}
            appt = pending["appointments"][idx]
            return await self._after_appt_selected(state, pending["action"], appt, pending.get("orig_text", text), t0)
        low = text.lower()
        if re.search(r"\bcancel\w*", low):
            action = "cancel"
        elif re.search(r"\b(reschedul\w*|move|change|postpone|prepone|shift)\b", low) and not pending.get("doctor_id"):
            action = "reschedule"
        elif re.search(r"\b(my appointments?|upcoming|when is my|do i have an? appointment|list)\b", low) and not re.search(r"\bbook\b", low):
            action = "list"
        else:
            action = "book"
        if action == "list":
            r = await self._call("booking", "list_my_appointments", {}, state)
            up = r.get("upcoming", [])
            body = "You have no upcoming appointments." if not up else "Your upcoming appointments:\n" + "\n".join(f"- {P.fmt_slot(a['start_at'])} with {a['doctor']} ({a['specialty']}, room {a['room']})" for a in up)
            return {"reply": body, "reply_kind": "template", "pending": None, "stages": self._stage("records", "Looking up your appointments", t0)}
        if action in {"cancel", "reschedule"}:
            r = await self._call("booking", "list_my_appointments", {}, state)
            up = r.get("upcoming", [])
            if not up:
                return {"reply": "I couldn't find any upcoming appointments for you. Would you like to book one?", "reply_kind": "template", "pending": None,
                        "stages": self._stage("records", "Looking up your appointments", t0)}
            if len(up) > 1:
                lst = "\n".join(f"{i}. {P.fmt_slot(a['start_at'])} with {a['doctor']}" for i, a in enumerate(up, 1))
                return {"reply": f"Which appointment would you like to {action}?\n{lst}", "reply_kind": "template",
                        "pending": {"flow": "booking", "step": "choose_appt", "action": action, "appointments": up, "orig_text": text},
                        "card": {"type": "appointments", "options": up}, "stages": self._stage("records", "Looking up your appointments", t0)}
            return await self._after_appt_selected(state, action, up[0], text, t0)
        return await self._offer_slots(state, text, pending, t0)

    async def _after_appt_selected(self, state: ChatState, action: str, appt: dict, text: str, t0: float) -> dict:
        if action == "cancel":
            confirm = {"action": "cancel", "appointment_id": appt["appointment_id"], "doctor": appt["doctor"], "specialty": appt["specialty"], "start_at": appt["start_at"], "when": P.fmt_slot(appt["start_at"])}
            return {"confirm": confirm, "pending": {"flow": "booking", "step": "confirm"}, "stages": self._stage("records", "Looking up your appointments", t0)}
        return await self._offer_slots(state, text, {"doctor_id": appt["doctor_id"], "reschedule_of": appt}, t0)

    def _doctor_from_text(self, text: str) -> str | None:
        if d := P.find_doctor_by_name(text):
            return d
        low = text.lower()
        for spec, aliases in SPECIALTY_ALIASES.items():
            if re.search(rf"\b{re.escape(spec.lower())}\b", low) or any(re.search(rf"\b{re.escape(a)}", low) for a in aliases if len(a) > 3 or a in {"ent", "gp", "bp"}):
                return spec
        return None

    async def _offer_slots(self, state: ChatState, text: str, pending: dict, t0: float) -> dict:
        doctor_ref = pending.get("doctor_id") or self._doctor_from_text(text)
        if not doctor_ref and pending.get("specialty"):
            doctor_ref = pending["specialty"]
        if not doctor_ref and P.SAME_DOCTOR.search(text):
            past = (await self._call("booking", "list_my_appointments", {}, state)).get("past", [])
            done = [a for a in past if a["status"] == "completed"]
            if done:
                doctor_ref = done[-1]["doctor_id"]
        if not doctor_ref:
            doctor_ref = await self._recall(state, "preferred_doctor")
        if not doctor_ref:
            docs = (await self._call("booking", "find_doctors", {}, state)).get("doctors", [])
            lst = "\n".join(f"- {d['name']} ({d['specialty']})" for d in docs)
            return {"reply": f"Which doctor or department would you like to see?\n{lst}", "reply_kind": "template",
                    "pending": {"flow": "booking", "step": "need_doctor", "reason": pending.get("reason")}, "stages": self._stage("records", "Finding doctors", t0)}
        if re.fullmatch(r"D0\d", str(doctor_ref)):
            doctor_id = doctor_ref
        else:
            found = (await self._call("booking", "find_doctors", {"specialty": doctor_ref}, state)).get("doctors", [])
            if not found:
                return {"reply": "I couldn't find that doctor or department. Which doctor would you like?", "reply_kind": "template",
                        "pending": {"flow": "booking", "step": "need_doctor"}, "stages": self._stage("records", "Finding doctors", t0)}
            doctor_id = found[0]["doctor_id"]
        today = self.clock().date()
        rng = P.parse_date_pref(text, today)
        start = rng.start if rng else today
        end = rng.end if rng else today + timedelta(days=6)
        tpref = P.parse_time_pref(text)
        after = pending.get("after")
        r = await self._call("booking", "get_slots", {"doctor_id": doctor_id, "date_from": start.isoformat(), "date_to": end.isoformat()}, state)
        slots = r.get("slots", [])
        if tpref:
            slots = [s for s in slots if tpref[0] <= s["start_at"][11:16] < tpref[1]] or slots
        if after:
            slots = [s for s in slots if s["start_at"] > after]
        note = ""
        if not slots:
            r = await self._call("booking", "get_slots", {"doctor_id": doctor_id, "date_from": (end + timedelta(days=1)).isoformat(), "date_to": (end + timedelta(days=8)).isoformat()}, state)
            slots = [s for s in r.get("slots", []) if not after or s["start_at"] > after]
            label = rng.label if rng else "that time"
            note = f"There are no free slots with {r.get('doctor', 'that doctor')} on {label}. "
        if not slots:
            return {"reply": note + "I couldn't find any free slots in the next two weeks. Would you like me to connect you to the front desk?", "reply_kind": "template",
                    "pending": None, "stages": self._stage("records", "Checking open slots", t0)}
        options = self._spread(slots, 3)
        reason = pending.get("reason") or self._reason_from(text)
        lst = "\n".join(f"{i}. {P.fmt_slot(s['start_at'])}" for i, s in enumerate(options, 1))
        doc_name = r.get("doctor")
        verb = "move your appointment to" if pending.get("reschedule_of") else "book"
        return {"reply": f"{note}Here are the next available times with {doc_name} ({r.get('specialty')}):\n{lst}\nWhich one should I {verb}? Reply with the number.",
                "reply_kind": "template", "card": {"type": "slots", "doctor": doc_name, "options": [{"n": i, "slot_id": s["slot_id"], "start_at": s["start_at"], "label": P.fmt_slot(s["start_at"])} for i, s in enumerate(options, 1)]},
                "pending": {"flow": "booking", "step": "choose", "doctor_id": doctor_id, "doctor": doc_name, "specialty": r.get("specialty"), "options": options, "reason": reason,
                            "reschedule_of": pending.get("reschedule_of")},
                "stages": self._stage("records", "Checking open slots", t0)}

    @staticmethod
    def _spread(slots: list[dict], n: int) -> list[dict]:
        """Up to n options, preferring different days/times rather than three adjacent slots."""
        out: list[dict] = []
        for s in slots:
            if all(abs(datetime.fromisoformat(s["start_at"]) - datetime.fromisoformat(o["start_at"])) >= timedelta(minutes=45) for o in out):
                out.append(s)
            if len(out) == n:
                break
        return out or slots[:n]

    @staticmethod
    def _reason_from(text: str) -> str:
        m = RX["triage"].search(text)
        return f"Consultation ({m.group(0).lower()})" if m else "Consultation"

    async def _booking_choose(self, state: ChatState, pending: dict, t0: float) -> dict:
        text = state["text"]
        options = pending["options"]
        if P.CHANGE.search(text) or P.parse_date_pref(text, self.clock().date()):
            nxt = {**pending, "after": None if P.parse_date_pref(text, self.clock().date()) else options[-1]["start_at"]}
            return await self._offer_slots(state, text, nxt, t0)
        if P.yes_no(text) == "no":
            return {"reply": "Okay, I haven't booked anything. Anything else I can help with?", "reply_kind": "template", "pending": None, "stages": self._stage("records", "Updating", t0)}
        idx = P.parse_choice(text, len(options), [o["start_at"] for o in options])
        if idx is None:
            return {"reply": f"Please reply with a number from 1 to {len(options)}, or say 'other times'.", "reply_kind": "template", "pending": pending,
                    "stages": self._stage("records", "Checking open slots", t0)}
        slot = options[idx]
        old = pending.get("reschedule_of")
        confirm = {"action": "reschedule" if old else "book", "slot_id": slot["slot_id"], "start_at": slot["start_at"], "when": P.fmt_slot(slot["start_at"]),
                   "doctor": pending.get("doctor"), "specialty": pending.get("specialty"), "doctor_id": pending.get("doctor_id"), "reason": pending.get("reason") or "Consultation"}
        if old:
            confirm.update(appointment_id=old["appointment_id"], previous=P.fmt_slot(old["start_at"]))
        return {"confirm": confirm, "pending": {**pending, "step": "confirm"}, "stages": self._stage("records", "Preparing confirmation", t0)}

    def after_booking(self, state: ChatState) -> str:
        if state.get("confirm"):
            return "confirm"
        return "output_guard"

    async def confirm(self, state: ChatState) -> dict:
        """Human-in-the-loop: pause the graph and show a confirmation card. Nothing is written before this."""
        card = {"type": "confirm", **state["confirm"]}
        decision = interrupt(card)
        value = decision.get("decision") if isinstance(decision, dict) else str(decision)
        return {"decision": value if value in {"confirm", "change", "cancel"} else "cancel"}

    def after_confirm(self, state: ChatState) -> str:
        return {"confirm": "commit", "change": "change"}.get(state.get("decision", "cancel"), "abort")

    async def commit(self, state: ChatState) -> dict:
        t0 = time.perf_counter()
        c = state["confirm"]
        if c["action"] == "book":
            r = await self._call("booking", "book_appointment", {"slot_id": c["slot_id"], "reason": c.get("reason", "")}, state)
        elif c["action"] == "reschedule":
            r = await self._call("booking", "reschedule_appointment", {"appointment_id": c["appointment_id"], "new_slot_id": c["slot_id"]}, state)
        else:
            r = await self._call("booking", "cancel_appointment", {"appointment_id": c["appointment_id"]}, state)
        stage = self._stage("write_db", "Saving to the clinic system", t0)
        if not r.get("ok"):
            return {"reply": f"{r.get('message', 'Sorry, that did not work.')}", "reply_kind": "template", "confirm": None, "pending": None, "stages": stage,
                    "card": {"type": "result", "ok": False, "error": r.get("error")}}
        a = r["appointment"]  # re-read from the database by the tool: confirm only what is stored
        if a.get("doctor_id") and c["action"] != "cancel":
            await self._remember(state, "preferred_doctor", a["doctor_id"])
            await self._remember(state, "preferred_doctor_name", a["doctor"])
        when = P.fmt_slot(a["start_at"])
        if c["action"] == "cancel":
            msg = f"Done. Your appointment with {a['doctor']} on {when} is cancelled (reference #{a['appointment_id']})."
        elif c["action"] == "reschedule":
            msg = f"Done. Your appointment is now on {when} with {a['doctor']}, room {a['room']} (reference #{a['appointment_id']}). Please arrive 15 minutes early."
        else:
            msg = f"Booked. {when} with {a['doctor']} ({a['specialty']}), room {a['room']}. Reference #{a['appointment_id']}. Please arrive 15 minutes early."
        return {"reply": msg, "reply_kind": "template", "confirm": None, "pending": None, "stages": stage,
                "card": {"type": "result", "ok": True, "action": c["action"], "appointment": a}}

    async def change(self, state: ChatState) -> dict:
        t0 = time.perf_counter()
        c = state["confirm"]
        pending = state.get("pending") or {}
        if c["action"] == "cancel":
            return {"reply": "Okay, I've kept your appointment as it is.", "reply_kind": "template", "confirm": None, "pending": None, "stages": self._stage("records", "Updating", t0)}
        nxt = {**pending, "after": c["start_at"]}
        out = await self._offer_slots({**state, "text": ""}, "", nxt, t0)
        return {**out, "confirm": None}

    async def abort(self, state: ChatState) -> dict:
        c = state.get("confirm") or {}
        what = "kept your appointment as it is" if c.get("action") == "cancel" else "haven't changed anything"
        return {"reply": f"Okay, I {what}. Anything else I can help with?", "reply_kind": "template", "confirm": None, "pending": None}

    # ------------------------------------------------------------- 6 prescription
    async def prescription(self, state: ChatState) -> dict:
        t0 = time.perf_counter()
        text = state["text"]
        esc = [m.group(0) for m in ESCALATE_RX.finditer(text)]
        if esc and FOOD_QUESTION.search(text) and all(SOFT_ESCALATE.match(e) for e in esc):
            esc = []  # "can I take it with food?" is answered from the record, not escalated
        if esc:
            severe = bool(SEVERE_REACTION.search(text))
            summary = f"Pharmacist question: {text[:160]}\nCareHarbor AI did not advise; patient asked about changing/adding/stopping medicine.\nPriority {'high (possible reaction)' if severe else 'normal'}."
            t = await self._call("prescription", "create_handoff", {"summary": summary, "priority": "high" if severe else "normal", "kind": "pharmacist"}, state)
            extra = " If you have swelling of the face or lips, trouble breathing or feel faint, call 112 / 108 now." if severe else ""
            return {"reply": f"I can't advise on stopping, changing, combining or adding medicines - that has to come from your doctor or pharmacist. "
                             f"I've asked our pharmacist to contact you (ticket #{t.get('ticket_id')}).{extra}",
                    "reply_kind": "template", "pending": {"flow": "prescription", "step": "followup"}, "card": {"type": "handoff", "ticket_id": t.get("ticket_id"), "kind": "pharmacist"},
                    "stages": self._stage("escalate", "Passing to the pharmacist", t0)}
        gate = await self._require_identity(state, "prescription")
        if gate:
            return gate
        r = await self._call("prescription", "get_prescriptions", {}, state)
        rx = r.get("prescriptions", [])
        stages = self._stage("records", "Looking up your prescriptions", t0)
        if not rx:
            return {"reply": "I couldn't find any prescriptions in your records. If you think this is wrong, I can connect you to our staff.", "reply_kind": "template", "pending": None, "stages": stages}
        chosen = self._select_rx(text, rx)
        lines = [self._explain_rx(p) for p in chosen]
        allergy = f"\nRecorded allergies: {r.get('allergies')}." if r.get("allergies") and r.get("allergies") != "None known" else ""
        body = "Here is what your doctor prescribed, exactly as recorded:\n" + "\n".join(lines) + allergy + \
               "\n\nI can't change doses or suggest other medicines. For questions about stopping or changing a medicine, I can connect you to our pharmacist."
        return {"reply": body, "reply_kind": "record", "grounding": str(chosen), "pending": {"flow": "prescription", "step": "followup"}, "stages": stages,
                "card": {"type": "prescriptions", "items": [{k: p[k] for k in ("drug", "strength", "form", "dose", "frequency", "times", "food", "duration_days", "start_date", "end_date", "doctor")} for p in chosen]}}

    @staticmethod
    def _select_rx(text: str, rx: list[dict]) -> list[dict]:
        low = text.lower()
        named = [p for p in rx if p["drug"].lower().split()[0] in low]
        if named:
            return named
        for cls, pattern in CLASS_WORDS.items():
            if re.search(pattern, low):
                hit = [p for p in rx if p["drug"].lower() in DRUG_CLASSES[cls]]
                if hit:
                    return hit
        active = [p for p in rx if p.get("active")]
        return active or rx

    @staticmethod
    def _explain_rx(p: dict) -> str:
        start, end = date.fromisoformat(p["start_date"]), date.fromisoformat(p["end_date"])
        dur = f"for {p['duration_days']} days ({start.day} {start:%b} to {end.day} {end:%b})" if p["duration_days"] < 90 else f"as a long-term medicine (current supply {start.day} {start:%b} to {end.day} {end:%b})"
        food = "" if p["food"] == "not applicable" else f", {p['food']}"
        note = f" Note: {p['notes']}" if p.get("notes") else ""
        status = "" if p.get("active") else " [course finished]"
        return f"- {p['drug']} {p['strength']} {p['form']}: {p['dose']}, {p['frequency']} ({p['times']}){food}, {dur}. Prescribed by {p['doctor']}.{note}{status}"

    # ------------------------------------------------------------- 7 faq
    async def faq(self, state: ChatState) -> dict:
        t0 = time.perf_counter()
        raw = state["text"]
        text, rewritten = contextualize(raw, state.get("last_faq"))  # conversational query rewriting
        who = principal_for_chat(state["session_id"], await self._verified(state))
        r = retrieve(text, "faq", llm=self._llm(state), who=who)
        stages = self._stage("retrieve", "Checking clinic information", t0, docs=[h.doc_id for h in r.hits][:2], grade=r.grade,
                             pipeline=r.pipeline, corrective=r.corrective, rewritten=text if rewritten else "")
        if not r.hits or r.grade == "low":  # CRAG-style gate: weak evidence -> refuse instead of guessing
            log_retrieval(state["session_id"], who, text, r.hits, "not_found_low_confidence", self.db_path, grade=r.grade)
            return {"reply": NOT_FOUND_REPLY, "reply_kind": "template", "pending": None, "stages": stages, "card": {"type": "out_of_scope"}}
        idx = get_index()
        context = sanitise_retrieved("\n\n".join(parent_context(h, idx)[:900] for h in r.hits[:3])).replace("**", "")  # small-to-big
        model_name = state.get("model") or get_settings().llm_model
        version = get_index().version
        cached = faq_cache.get(text, version, model_name)
        if cached:
            log_retrieval(state["session_id"], who, text, r.hits, "answered_from_cache", self.db_path, similarity=cached["cache"]["similarity"])
            return {"reply": cached["reply"], "reply_kind": "llm_checked", "citations": cached["citations"], "pending": None, "last_faq": raw,
                    "evidence": attribute(cached["reply"], r.hits[:3]),
                    "stages": stages + self._stage("cache", "Answer cache hit", t0, similarity=cached["cache"]["similarity"])}
        t1 = time.perf_counter()
        system, user = TEMPLATES["faq"].render(context=context, question=text)
        reply = await self._llm(state).achat(system, user, task="answer", max_tokens=120)
        stages += self._stage("write", "Writing the answer", t1)
        usage = [{"task": "faq_answer", "model": reply.model, "in": reply.prompt_tokens, "out": reply.completion_tokens, "ms": reply.ms}]
        body = reply.text.strip()
        if "NOT_FOUND" in body:
            log_retrieval(state["session_id"], who, text, r.hits, "not_found_llm", self.db_path)
            return {"reply": NOT_FOUND_REPLY, "reply_kind": "template", "pending": None, "stages": stages, "llm_usage": usage, "card": {"type": "out_of_scope"}}
        check = check_output(body, kind="llm", grounding=context + " " + text)
        guard = []
        if not check.ok or not body.strip():
            guard = [{"type": "output_guard", "violations": check.violations, "details": check.details[:3], "action": "extractive_fallback"}]
            self._audit(state["session_id"], "guardrail", "output_blocked", violations=check.violations)
            top = r.hits[0]
            body = f"From our {top.title.lower()} information:\n" + self._relevant_lines(top.text, text)
        log_retrieval(state["session_id"], who, text, r.hits, "answered_fallback" if guard else "answered", self.db_path)
        if not guard and len(body.split()) < 4:
            # A bare "No"/"Yes" is unhelpful: add the supporting source line verbatim (still fully grounded). A small model can also
            # get the polarity wrong ("Is a follow-up free?" -> "No" while the policy says free within 7 days), so if the verdict
            # contradicts the source line's polarity, drop the verdict and answer with the source line alone.
            line = self._relevant_lines(r.hits[0].text, text, 1).lstrip("-* ").replace("**", "")
            verdict = body.rstrip(".").strip()
            if self._polarity_conflict(verdict, line):
                guard = guard + [{"type": "polarity_check", "dropped": verdict}]
                body = f"From \"{r.hits[0].section}\": {line}"
            else:
                body = f"{verdict}. From \"{r.hits[0].section}\": {line}"
        top_score = r.hits[0].score or 1
        cited = [c for h, c in zip(r.hits, r.citations) if h.score >= 0.35 * top_score] or r.citations[:1]
        if not guard:  # only clean, grounded answers are cached
            faq_cache.put(text, version, model_name, {"reply": body, "citations": cited})
        return {"reply": body, "reply_kind": "llm_checked", "grounding": context + " " + text, "citations": cited, "pending": None,
                "stages": stages, "llm_usage": usage, "guardrails": guard, "last_faq": raw, "evidence": attribute(body, r.hits[:3])}

    _NEGATIVE = re.compile(r"(?i)\b(not|no|never|closed|cannot|can't|isn't|aren't|don't|doesn't|won't|unavailable|excluded)\b")

    @classmethod
    def _polarity_conflict(cls, verdict: str, line: str) -> bool:
        """True when a bare yes/no verdict disagrees with the source line (negative wording <-> 'no')."""
        v = verdict.lower().strip(" .!")
        if v not in {"yes", "no", "yes it is", "no it is not", "yes we do", "no we don't", "no we do not"}:
            return False
        return v.startswith("no") != bool(cls._NEGATIVE.search(line))

    @staticmethod
    def _relevant_lines(section_text: str, question: str, n: int = 3) -> str:
        """Extractive fallback: quote the source lines that best match the question, in their original order (never cut mid-sentence)."""
        pre = lambda s: {w[:5] for w in re.findall(r"[a-z]+", s.lower()) if len(w) >= 3}  # noqa: E731
        q = pre(question) - pre("the and you your what are can for how does with this that")
        lines = [ln.strip() for ln in section_text.splitlines() if ln.strip() and not re.fullmatch(r"[|\s:-]+", ln)]
        ranked = sorted(range(len(lines)), key=lambda i: -len(q & pre(lines[i])))[:n]
        return "\n".join(lines[i] for i in sorted(ranked))

    # ------------------------------------------------------------- 8 handoff / smalltalk / blocked
    async def handoff(self, state: ChatState) -> dict:
        t0 = time.perf_counter()
        last_bot = next((m["content"] for m in reversed(state.get("messages") or []) if m["role"] == "assistant"), "")
        complaint = bool(re.search(r"(?i)complain|unhappy|angry|rude|wrong|mistake", state["text"]))
        summary = "\n".join([
            f"Patient asked for a person: \"{state['text'][:120]}\"",
            f"Last topic: {(state.get('pending') or {}).get('flow') or 'general'}; last CareHarbor AI reply: {last_bot[:80] or '-'}",
            "Please call back or reply in chat." + (" Possible complaint." if complaint else ""),
        ])
        t = await self._call("handoff", "create_handoff", {"summary": summary, "priority": "high" if complaint else "normal", "kind": "handoff"}, state)
        return {"reply": f"I've passed this to our front-desk team (ticket #{t.get('ticket_id')}). Someone will get back to you shortly during OPD hours (Monday to Saturday, 8 AM to 6 PM).",
                "reply_kind": "template", "pending": None, "card": {"type": "handoff", "ticket_id": t.get("ticket_id"), "kind": "handoff"},
                "stages": self._stage("handoff", "Contacting staff", t0)}

    async def smalltalk(self, state: ChatState) -> dict:
        t = state["text"].lower()
        if P.yes_no(t) == "no":
            reply = "Okay. Take care, and come back if anything changes. In an emergency, call 112 / 108."
        elif re.search(r"\b(thank|thanks|thx)\b", t):
            reply = "You're welcome! Is there anything else I can help with?"
        elif re.search(r"\bbye\b", t):
            reply = "Take care! Remember: in an emergency, call 112 / 108."
        else:
            reply = ("Hello! I'm CareHarbor AI, the clinic's AI front desk. I can:\n- guide you on symptoms (safely, with our care leaflets)\n- book, reschedule or cancel appointments\n"
                     "- explain your prescriptions\n- answer questions about timings, fees and insurance\n- connect you to a person\nHow can I help?")
        return {"reply": reply, "reply_kind": "template", "pending": state.get("pending")}

    async def blocked(self, state: ChatState) -> dict:
        return {"reply": "I can only help with your own care and general clinic questions. I can't change how I work or share anyone else's information.",
                "reply_kind": "template", "pending": None, "card": {"type": "blocked"}}

    async def out_of_scope(self, state: ChatState) -> dict:
        return {"reply": SCOPE_REPLY, "reply_kind": "template", "pending": None, "card": {"type": "out_of_scope"}}

    async def forget(self, state: ChatState) -> dict:
        """Right to erasure (DPDP Act): delete the long-term memory kept for this patient/session."""
        if self.store is not None:
            for ns in ([("patients", state["memory_key"])] if state.get("memory_key") else []) + [("sessions", state["session_id"])]:
                await self.store.adelete(ns, "prefs")
        self._audit(state["session_id"], "patient", "memory.erased", scope="patient" if state.get("memory_key") else "session")
        return {"reply": "Done - I've erased the preferences I remembered for you (such as your usual doctor). Your medical records at the clinic are not affected.",
                "reply_kind": "template", "pending": None, "card": {"type": "memory_erased"}}

    # ------------------------------------------------------------- 9 output guard + finalize
    async def output_guard(self, state: ChatState) -> dict:
        t0 = time.perf_counter()
        reply = state.get("reply") or ""
        kind = state.get("reply_kind", "template")
        if kind in {"emergency", "llm_checked"}:  # llm_checked: already checked inside the agent against its grounding
            return {"stages": self._stage("guard", "Final safety check", t0)}
        check = check_output(reply, kind="record" if kind in {"record", "template"} else "llm", grounding=state.get("grounding", "") if kind == "record" else "")
        if check.ok:
            return {"stages": self._stage("guard", "Final safety check", t0)}
        self._audit(state["session_id"], "guardrail", "final_output_blocked", violations=check.violations, kind=kind)
        return {"reply": SAFE_FALLBACK, "reply_kind": "template", "card": {"type": "blocked"},
                "guardrails": [{"type": "output_guard", "violations": check.violations, "details": check.details[:3], "action": "safe_fallback"}],
                "stages": self._stage("guard", "Final safety check", t0)}

    async def finalize(self, state: ChatState) -> dict:
        from careharbor.logging_setup import redact

        reply = (state.get("prefix") or "") + (state.get("reply") or "")
        # The checkpoint is data at rest: keep only a PII-masked copy of the raw message once the turn is done.
        return {"reply": reply, "prefix": "", "resume": None, "text": redact(state.get("text") or ""), "messages": [{"role": "assistant", "content": reply}]}

    # ------------------------------------------------------------- build
    def build(self, checkpointer: Any = None) -> Any:
        g = StateGraph(ChatState)
        for name in ["safety_gate", "emergency", "router", "identity", "triage", "booking", "confirm", "commit", "change", "abort",
                     "prescription", "faq", "handoff", "smalltalk", "blocked", "out_of_scope", "forget", "output_guard", "finalize"]:
            g.add_node(name, getattr(self, name))
        g.add_edge(START, "safety_gate")
        g.add_conditional_edges("safety_gate", self.after_safety, {"emergency": "emergency", "router": "router"})
        g.add_conditional_edges("router", self.after_router, {i: i for i in ["identity", "triage", "booking", "prescription", "faq", "handoff", "smalltalk", "blocked", "out_of_scope", "forget", "emergency"]})
        g.add_conditional_edges("identity", self.after_identity, {"booking": "booking", "prescription": "prescription", "output_guard": "output_guard"})
        g.add_conditional_edges("triage", self.after_triage, {"emergency": "emergency", "output_guard": "output_guard"})
        g.add_conditional_edges("booking", self.after_booking, {"confirm": "confirm", "output_guard": "output_guard"})
        g.add_conditional_edges("confirm", self.after_confirm, {"commit": "commit", "change": "change", "abort": "abort"})
        for n in ["commit", "change", "abort", "prescription", "faq", "handoff", "smalltalk", "blocked", "out_of_scope", "forget"]:
            g.add_edge(n, "output_guard")
        g.add_edge("emergency", "finalize")
        g.add_edge("output_guard", "finalize")
        g.add_edge("finalize", END)
        return g.compile(checkpointer=checkpointer, store=self.store)
