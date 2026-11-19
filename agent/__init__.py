"""agent/__init__.py - local autonomous agent runtime, no cloud API.

Public API:
    Agent, Tool, ToolBus, ToolError, Memory, Planner, Model, LocalModel, Message
    build_agent, default_tools
"""

from .core import (
    Agent,
    LocalModel,
    Memory,
    Message,
    Model,
    Plan,
    Planner,
    Step,
    SYSTEM_PROMPT,
    Tool,
    ToolBus,
    ToolError,
    build_agent,
    parse_calls,
    render,
)
from .tools import default_tools

__all__ = [
    "Agent", "LocalModel", "Memory", "Message", "Model", "Plan", "Planner",
    "Step", "SYSTEM_PROMPT", "Tool", "ToolBus", "ToolError", "build_agent",
    "default_tools", "parse_calls", "render",
]

__version__ = "1.0"
