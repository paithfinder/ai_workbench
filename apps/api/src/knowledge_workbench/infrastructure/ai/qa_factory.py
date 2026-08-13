from __future__ import annotations

from knowledge_workbench.application.ports.ai_gateway import AIGateway
from knowledge_workbench.config import Settings
from knowledge_workbench.infrastructure.ai.claude import ClaudeAIGateway
from knowledge_workbench.infrastructure.ai.fake import FakeAIGateway


def create_qa_gateway(settings: Settings) -> AIGateway:
    if settings.ai_provider == "anthropic":
        return ClaudeAIGateway(
            model=settings.claude_model,
            timeout_seconds=settings.qa_timeout_seconds,
            max_tokens=settings.qa_max_output_tokens,
        )
    return FakeAIGateway(
        {
            "answer_qa_turn": lambda _: {
                "status": "abstained",
                "answer": None,
                "abstain_code": "insufficient_evidence",
                "claims": [],
            }
        }
    )
