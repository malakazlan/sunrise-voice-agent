"""Every tool the agent has, registered in one place.

`register_tools` is the one entry point: it registers each tool on the application's registry
and seals it, after which nothing more can be registered. The application calls it at start;
the structural tests call it too, so every tool registered here is bound by the gate's rules.
"""

from app.agent.serving import SERVING_TOOLS
from app.agent.tool_registry import REGISTRY, Registry


def register_tools() -> Registry:
    """Register every tool and seal the registry. Calling it again returns the sealed one."""
    if not REGISTRY.sealed:
        for tool in SERVING_TOOLS:
            REGISTRY.register(tool)
        REGISTRY.seal()
    return REGISTRY
