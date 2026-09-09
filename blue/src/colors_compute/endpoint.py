"""Packaged endpoint agent artifact; does not perform endpoint operations."""
import json
from importlib.resources import files

def endpoint_agent(provider):
    recipes = json.loads(files('colors_compute').joinpath('provider-recipes.json').read_text())
    if not isinstance(provider, str) or not recipes.get(provider, {}).get('application_reserved_ip'):
        raise ValueError('unsupported compute endpoint capability')
    return {'filename': 'colors-compute-endpoint',
            'content': files('colors_compute').joinpath('endpoint-agent.py').read_text(),
            'credentials': recipes[provider]['endpoint_credentials']}
