"""Single compute-unit provisioning for Colors SDK callers."""
from .node import node_plan, build_node, compute_node, registration_plan, build_registration, compute_registration
from .contract import credential_requirements, compute_credential_errors, validate, registry
from .rendering import backend_plan, provider_plan, render_template
from .provider_request import provider_request
from .endpoint import endpoint_agent
from .controller import controller_artifact

__all__ = ["ssh_plan", "ssh_resource", "start_agent", "registration_plan", "build_registration", "compute_registration", "node_plan", "build_node", "compute_node", "credential_requirements",
           "compute_credential_errors", "validate", "registry", "backend_plan",
           "provider_plan", "render_template", "provider_request", "endpoint_agent",
           "controller_artifact"]

from .ssh import ssh_plan, ssh_resource, start_agent
