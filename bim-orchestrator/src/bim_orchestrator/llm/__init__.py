"""Provider-agnostic LLM seam for Phase 2 agents.

Phase 1 runtime is deterministic and LLM-free. Everything in this package is
the *judgment* layer of the neuro-symbolic split: agents that need generative
reasoning depend on the ``LLMClient`` interface here, never on a concrete SDK.

Swapping cloud (Anthropic) for local (Ollama / Qwen / Gemma) is a constructor
change, not an agent change. Tests inject ``FakeLLMClient`` to stay offline and
deterministic — the 870-test Phase 1 posture is preserved.
"""

from .anthropic_client import AnthropicLLMClient
from .client import LLMClient, LLMError, sanitize_json_schema
from .fake import FakeLLMClient
from .ollama_client import OllamaLLMClient

__all__ = [
    "LLMClient",
    "LLMError",
    "FakeLLMClient",
    "AnthropicLLMClient",
    "OllamaLLMClient",
    "sanitize_json_schema",
]
