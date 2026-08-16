from __future__ import annotations

from knowledge_workbench.application.ports.ai_gateway import AIGateway
from knowledge_workbench.config import Settings
from knowledge_workbench.infrastructure.ai.claude import ClaudeAIGateway
from knowledge_workbench.infrastructure.ai.fake import FakeAIGateway


def create_proposal_comparison_gateway(settings: Settings) -> AIGateway:
    if settings.ai_provider == "anthropic":
        return ClaudeAIGateway(
            model=settings.claude_model,
            timeout_seconds=settings.proposal_comparison_timeout_seconds,
            max_tokens=settings.proposal_comparison_max_output_tokens,
        )
    return FakeAIGateway(
        {
            "generate_proposal_comparison": lambda _: {
                "comparison_kind": "no_evidence",
                "summary": "Insufficient evidence for a safe knowledge update proposal.",
                "uncertainty_reason": (
                    "The deterministic provider does not infer comparison conclusions."
                ),
            }
        }
    )
