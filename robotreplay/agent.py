import json
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context

from robotreplay.embeddings import hybrid_retrieve
from robotreplay.policy import PROMPTS, ModelSelection, input_policy
from robotreplay.provider import ModelUnavailable
from robotreplay.retrieval import retrieve


class Investigation(TypedDict, total=False):
    team: str
    clip_ids: list[str]
    question: str
    evidence: list[dict]
    strategy: str
    result: dict
    tool_calls: list[str]
    comparison: list[dict]
    concept: dict
    retrieval_mode: str


class Investigator:
    """Fixed, bounded graph. No shell, export, actuator, or write tool is exposed to a model."""

    def __init__(self, store, telemetry, provider, nemo=None):
        self.store, self.telemetry, self.provider, self.nemo = store, telemetry, provider, nemo
        graph = StateGraph(Investigation)
        graph.add_node("policy", self.check)
        graph.add_node("retrieve", self.lookup)
        graph.add_node("reason", self.reason)
        graph.add_edge(START, "policy")
        graph.add_conditional_edges("policy", lambda s: END if s.get("result") else "retrieve")
        graph.add_edge("retrieve", "reason")
        graph.add_edge("reason", END)
        self.graph = graph.compile()

    def compare_trials(self, clip_ids, team):
        return [
            {
                "clip_id": c["id"],
                "title": c["title"],
                "duration": c["duration"],
                "marker_coverage": c["coverage"],
                "event_count": len(self.store.evidence(c["id"], team)),
            }
            for cid in clip_ids
            if (c := self.store.clip(cid, team))
        ]

    def get_approved_concept(self):
        return json.loads((Path(__file__).parent / "data" / "concepts.json").read_text())[0]

    async def check(self, state):
        with self.telemetry.span("policy.input", state["team"]):
            decision = input_policy(state["question"])
            if self.nemo and decision.allowed:
                try:
                    if not await self.nemo.check(state["question"]):
                        decision = type(decision)(
                            False, "nemo_blocked", "Review the evidence with a teaching question."
                        )
                except Exception:
                    decision = type(decision)(
                        False,
                        "nemo_unavailable",
                        "The extra policy check is unavailable. Review the replay directly.",
                    )
            self.telemetry.policy.labels(decision.reason).inc()
            if not decision.allowed:
                self.store.audit(state["team"], "question", decision.reason)
                return {
                    "result": {
                        "status": "redirect",
                        "reason": decision.reason,
                        "question": decision.message,
                        "evidence": [],
                        "tool_calls": [],
                    }
                }
        return {}

    async def lookup(self, state):
        # Authorization checked here as well as at the API boundary.
        if any(not self.store.clip(cid, state["team"]) for cid in state["clip_ids"]):
            raise PermissionError("scope_mismatch")
        with self.telemetry.span("tools.get_trial_evidence", state["team"]):
            evidence, strategy, excluded = retrieve(
                self.store, state["team"], state["clip_ids"], state["question"]
            )
        calls = ["get_trial_evidence"]
        if state.get("retrieval_mode") == "hybrid":
            try:
                evidence = await hybrid_retrieve(
                    self.provider, state["team"], state["clip_ids"], state["question"], evidence
                )
                strategy = "hybrid_rrf"
            except ModelUnavailable as exc:
                strategy = "fts5_fallback:" + exc.reason
        comparison = []
        if len(state["clip_ids"]) == 2:
            with self.telemetry.span("tools.compare_trials", state["team"]):
                comparison = self.compare_trials(state["clip_ids"], state["team"])
                calls.append("compare_trials")
        with self.telemetry.span("tools.get_approved_concept", state["team"]):
            concept = self.get_approved_concept()
            calls.append("get_approved_concept")
        if excluded:
            self.store.audit(state["team"], "retrieval", "excluded_untrusted_source")
        return {
            "evidence": evidence,
            "strategy": strategy,
            "tool_calls": calls,
            "comparison": comparison,
            "concept": concept,
        }

    async def reason(self, state):
        evidence = [e for e in state["evidence"] if e["kind"] != "visibility_gap"]
        if not evidence:
            result = {
                "status": "abstain",
                "reason": "insufficient_evidence",
                "question": "There is no reliable visible event for this question. "
                "Add a checked timestamp or record a clearer view.",
                "evidence": [],
                "tool_calls": state["tool_calls"],
                "strategy": state["strategy"],
            }
        else:
            kinds = {e["kind"] for e in evidence}
            chosen = "compare_turn" if "turn_candidate" in kinds else "repeat_setup"
            selection = ModelSelection(
                question_id=chosen, evidence_ids=[e["id"] for e in evidence[:4]]
            )
            inference, reason = {"provider": "local"}, "local_templates"
            try:
                selection, inference = await self.provider.choose(
                    state["question"], evidence, state["team"]
                )
                reason = "model_selection"
            except ModelUnavailable as exc:
                reason = exc.reason
            with self.telemetry.span("policy.output", state["team"]):
                selection.verified({e["id"] for e in evidence})
                selected = [e for e in evidence if e["id"] in selection.evidence_ids]
                result = {
                    "status": "ready",
                    "reason": reason,
                    "question": PROMPTS[selection.question_id],
                    "evidence": selected,
                    "inference": inference,
                    "tool_calls": state["tool_calls"],
                    "strategy": state["strategy"],
                    "concept": state["concept"],
                    "comparison": state["comparison"],
                }
        self.store.audit(state["team"], "question", result["reason"])
        return {"result": result}

    async def ask(self, team, clip_ids, question, retrieval_mode="lexical"):
        # Never let ambient LangSmith settings export private graph inputs.
        with tracing_context(enabled=False):
            state = await self.graph.ainvoke(
                {
                    "team": team,
                    "clip_ids": clip_ids,
                    "question": question,
                    "tool_calls": [],
                    "retrieval_mode": retrieval_mode,
                },
                config={"recursion_limit": 8},
            )
        return state["result"]
