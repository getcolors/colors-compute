"""Authored lifecycle errors and conservative, bounded command diagnostics."""
import base64
from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import quote, quote_plus

MESSAGES = {
    'command_failed': 'Required command failed.',
    'missing_credentials': 'Required credentials are not set.',
    'state_unreadable': 'Compute state could not be read.',
    'state_absent': 'Required compute state is absent.',
    'identity_mismatch': 'Compute state identity does not match the requested node.',
    'unsafe_plan': 'Compute plan requires an unauthorized change.',
    'invalid_request': 'Invalid compute request.',
    'key_access_failed': 'SSH key access could not be prepared.',
    'filesystem_error': 'Compute working files could not be accessed.',
    'internal_error': 'Compute operation failed.',
}


class NodeError(Exception):
    def __init__(self, code, **metadata):
        super().__init__(MESSAGES[code])
        self.code = code
        self.metadata = metadata


def credential_values(*sources):
    values = set()
    def visit(value, secret=False):
        if isinstance(value, Mapping):
            for key, item in value.items():
                visit(item, secret or bool(re.search(r'token|secret|password|credential|private.?key|access.?key|api.?key|authorization', str(key), re.I)))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                visit(item, secret)
        elif secret and isinstance(value, str) and value:
            values.add(value)
    for source in sources:
        visit(source)
    return values


def sanitize_stderr(value, secrets):
    if not isinstance(value, str):
        return ''
    value = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', value)
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    value = ''.join(char for char in value if char in '\n\t' or unicodedata.category(char) not in ('Cc', 'Cf'))
    value = re.sub(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)', '[private material suppressed]', value)
    for secret in sorted(secrets, key=len, reverse=True):
        variants = {secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1], quote(secret, safe=''), quote_plus(secret, safe=''), base64.b64encode(secret.encode()).decode(), base64.urlsafe_b64encode(secret.encode()).decode()}
        variants.update(re.sub(r'%[0-9A-F]{2}', lambda match: match[0].lower(), value) for value in tuple(variants))
        for variant in sorted(variants, key=len, reverse=True):
            if variant:
                value = value.replace(variant, '[REDACTED]')
    value = re.sub(r"(?im)(\b(?:[A-Za-z0-9_-]*(?:token|secret|password|private[_-]?key|access[_-]?key|api[_-]?key)[A-Za-z0-9_-]*|authorization)\b[\"']?\s*[=:]\s*)[^\n]*", r'\1[REDACTED]', value)
    value = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [REDACTED]', value)
    if re.search(r'(?:\{\s*[:"\'}]|\[\s*["\'{\[]|"(?:resources|outputs|planned_values|resource_changes|private_key)"\s*:)', value):
        return '[structured output suppressed]'
    return value[:2000]


def command_metadata(args, environment, directory, result=None):
    executable = str(args[0])
    name = Path(executable).name
    if name == 'tofu':
        command = [name, *args[1:3]] if args[1:2] == ['state'] else [name, *args[1:2]]
    elif name in ('aws', 'gcloud'):
        command = [name, *args[1:3]]
    elif name == 'ssh-keygen':
        command = [name]
    else:
        command = [name]
    candidates = ([Path(directory) / executable] if '/' in executable else
                  [(Path(part) if Path(part).is_absolute() else Path(directory) / part) / executable
                   for part in (environment['PATH'].split(os.pathsep) if 'PATH' in environment else [])])
    resolved = next((os.path.abspath(path) for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)
    metadata = {'command': command}
    if resolved:
        metadata['executable'] = resolved
    if result is not None:
        if type(result.exit) is int:
            metadata['exit_code'] = result.exit
        metadata['stderr'] = result.err
    return metadata


def classify(error, stage):
    if isinstance(error, NodeError):
        return error.code
    if isinstance(error, OSError):
        return 'filesystem_error'
    message = str(error)
    if isinstance(error, ValueError):
        if any(word in message for word in ('identity mismatch', 'identity changed', 'provider change', 'provider or unit change', 'remote key ownership', 'backend identity')):
            return 'identity_mismatch'
        if any(word in message for word in ('state missing', 'state is missing', 'state is absent', 'existing state required', 'state required')):
            return 'state_absent'
        if 'unsafe' in message and any(word in message for word in ('file', 'directory')):
            return 'filesystem_error'
    return {'validate': 'invalid_request', 'build': 'filesystem_error', 'credentials': 'missing_credentials',
            'state': 'state_unreadable', 'plan-validation': 'unsafe_plan', 'access': 'key_access_failed',
            'cleanup': 'filesystem_error'}.get(stage, 'internal_error')


def failure(error, stage, changes, secrets):
    code = classify(error, stage)
    detail = {'code': code, 'stage': stage, 'message': MESSAGES[code], 'infrastructure_changes': changes}
    if isinstance(error, NodeError):
        detail.update(error.metadata)
        if 'stderr' in detail:
            detail['stderr'] = sanitize_stderr(detail['stderr'], secrets)
        if 'executable' in detail and (any(secret in detail['executable'] for secret in secrets) or any(unicodedata.category(char) in ('Cc', 'Cf') for char in detail['executable'])):
            detail.pop('executable')
    return {'status': 'error', 'error': detail}
