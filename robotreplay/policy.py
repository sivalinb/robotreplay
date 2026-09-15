import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

POLICY_VERSION = "teach-v1"
PROMPTS = {
    "compare_turn": "What would you keep constant when comparing these visible turns?",
    "repeat_setup": "Which setup detail would you keep the same for the next attempt?",
    "measure_stop": "Which visible reference point would help you compare the stopping positions?",
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    message: str


def input_policy(text):
    normalized = " ".join(text.lower().split())
    if re.search(r"(api.?key|password|access.?token|secret|credential)", normalized):
        return Decision(
            False, "secret_request", "Credentials are unavailable through this workspace."
        )
    if re.search(
        r"(ignore.{0,50}(instruction|policy|coach)|export.{0,40}(all|other).{0,20}team|"
        r"(other|another) team.{0,40}(record|trial|data)|system prompt)",
        normalized,
    ):
        return Decision(
            False,
            "untrusted_instruction",
            "That instruction cannot change evidence access or tool permissions.",
        )
    if re.search(
        r"(write|generate|complete|finish|draft).{0,80}(notebook|report|homework)", normalized
    ):
        return Decision(
            False,
            "student_authorship",
            "What did you change, what did you observe, and which recording supports it? "
            "Describe those points in your own words.",
        )
    if "<" in text or ">" in text:
        return Decision(
            False, "unexpected_markup", "Use a plain-text question about the robot evidence."
        )
    return Decision(True, "allowed", "")


def redact(text):
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.\w+\b", "[email removed]", text)
    text = re.sub(r"\b(?:\+?\d[\d ()-]{8,}\d)\b", "[number removed]", text)
    return text


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str
    evidence_ids: list[str] = Field(min_length=1, max_length=6)

    def verified(self, allowed_ids):
        if self.question_id not in PROMPTS:
            raise ValueError("unknown_question")
        if not set(self.evidence_ids).issubset(allowed_ids):
            raise ValueError("unsupported_evidence")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("duplicate_evidence")
        return self


class NemoPolicy:
    """Optional extra policy layer. The application enforces authorization independently."""

    def __init__(self):
        from pathlib import Path

        from nemoguardrails import LLMRails, RailsConfig

        self.rails = LLMRails(RailsConfig.from_path(str(Path(__file__).parent / "rails")))

        async def robotreplay_input_allowed(context=None):
            return input_policy((context or {}).get("user_message", "")).allowed

        self.rails.register_action(robotreplay_input_allowed, name="robotreplay_input_allowed")

    async def check(self, text):
        # Only input rails execute. No generation model or embedding model is configured.
        result = await self.rails.generate_async(
            messages=[{"role": "user", "content": text}],
            options={"rails": ["input"]},
        )
        response = result.response if hasattr(result, "response") else result
        if isinstance(response, list):
            response = response[-1] if response else {}
        if isinstance(response, dict) and response.get("content") == "RR_INPUT_BLOCKED":
            return False
        return True
