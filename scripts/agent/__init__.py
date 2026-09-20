# Auto-install the LiteLLM gateway SSE compat shim BEFORE any submodule
# constructs an Anthropic client. See sdk_compat.py for context.
from . import sdk_compat  # noqa: F401
