"""Provider selection and node contracts shared by Colors package skills."""

from .contract import (
    collect, credential_requirements, compute_credential_errors, expand, state_decision, state_keys, validate,
)
from .journal import journal_get, journal_put
from .coordination import coordination
from .backend import read_state
from .rendering import backend_plan, provider_plan, render_template

__all__ = [
    "Coordinator", "provider_request", "prepare_keypair", "cleanup_keypair",
    "state_presence", "converge_state",
    "collect", "credential_requirements", "expand", "state_decision", "state_keys",
    "validate", "journal_get", "journal_put", "coordination", "read_state", "backend_plan", "provider_plan", "render_template",
]

from .coordinator import Coordinator

from .provider_request import provider_request
from .ssh import prepare_keypair, cleanup_keypair
from .execution import state_presence, converge_state
from .deployment_request import deployment_requests, source_cidrs
from .key_request import key_request
from .planning import plan_deployment
from .inspection import read_deployment
from .contract import registry
from .endpoint import endpoint_agent

__all__ += ['deployment_requests', 'source_cidrs', 'key_request', 'plan_deployment',
            'read_deployment', 'registry', 'orchestrate', 'endpoint_agent',
            'compute_credential_errors']


async def orchestrate(*args, **kwargs):
    # Pure contract consumers do not load the SDK workflow machinery.
    from .orchestration import orchestrate as run_deployment
    return await run_deployment(*args, **kwargs)

from .drift import check_deployment_drift
__all__.append('check_deployment_drift')

from .controller import controller_artifact
__all__.append("controller_artifact")

from .power import power_deployment
__all__.append("power_deployment")

from .managed_backend import bootstrap_backend, finalize_backend
__all__ += ["bootstrap_backend", "finalize_backend"]
