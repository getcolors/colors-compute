"""Provider selection and node contracts shared by Colors package skills."""

from .contract import (
    collect, credential_requirements, expand, state_decision, state_keys, validate,
)
from .coordination import coordination
from .backend import read_state
from .rendering import backend_plan, provider_plan, render_template

__all__ = [
    "collect", "credential_requirements", "expand", "state_decision", "state_keys",
    "validate", "coordination", "read_state", "backend_plan", "provider_plan", "render_template",
]
