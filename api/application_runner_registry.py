"""Deployment-owned immutable command registry, never request authority.

Construct only at the trusted composition root after release/profile acceptance.
An empty registry is the default; no command is enabled by mere availability.
"""
import os
import re
from dataclasses import dataclass
from types import MappingProxyType
from api.application_task_runner import RunnerBlocked


@dataclass(frozen=True)
class FixedTool:
    argv: tuple
    timeout_ms: int

    def __post_init__(self):
        if (type(self.argv) is not tuple or not self.argv
                or any(type(s) is not str or '\x00' in s for s in self.argv)
                or not os.path.isabs(self.argv[0])
                or type(self.timeout_ms) is not int or self.timeout_ms <= 0):
            raise RunnerBlocked('FIXED_TOOL_INVALID')


class ToolRegistry:
    def __init__(self, tools=None):
        tools = {} if tools is None else dict(tools)
        if any(type(k) is not str or not re.fullmatch('[a-z][a-z0-9_]{0,63}', k)
               or type(v) is not FixedTool for k,v in tools.items()):
            raise RunnerBlocked('TOOL_REGISTRY_INVALID')
        self._tools = MappingProxyType(tools)

    def resolve(self, tool_id):
        if type(tool_id) is not str or tool_id not in self._tools:
            raise RunnerBlocked('TOOL_NOT_REGISTERED')
        return self._tools[tool_id]
