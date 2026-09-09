"""Resolve explicitly selected public key references for provider requests."""
import base64
import hashlib
from ._copy import deepcopy
from importlib.resources import files
import json
import os
from pathlib import Path
import struct

from .contract import _missing, registry
from .ssh import PLACEHOLDER


def _public(value):
    if not isinstance(value, str):
        raise ValueError('invalid external SSH public key')
    value = value.strip()
    parts = value.split()
    if len(parts) < 2 or '\n' in value or '\r' in value or parts[0] not in ('ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256', 'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521'):
        raise ValueError('invalid external SSH public key')
    try:
        blob = base64.b64decode(parts[1], validate=True)
        length = struct.unpack('!I', blob[:4])[0]
        if blob[4:4+length] != parts[0].encode() or len(blob) <= 4 + length:
            raise ValueError()
    except (ValueError, struct.error):
        raise ValueError('invalid external SSH public key') from None
    return value


def key_request(opts, prepared, environment=None):
    """No private file is opened. Planning never opens a public file either."""
    if prepared.get('mode') == 'managed':
        return {'mode': 'managed', 'public_key': prepared['public_key']}
    if prepared.get('mode') != 'external':
        raise ValueError('invalid compute key request')
    recipes = json.loads(files('colors_compute').joinpath('provider-recipes.json').read_text())
    provider = opts.get('provider-compute')
    if not isinstance(provider, str) or provider not in recipes:
        raise ValueError('compute provider recipe unavailable')
    kind = registry()['compute'][provider].get('ssh-aliases', {}).get(prepared.get('setting'), recipes[provider]['external_key_kind'])
    reference = prepared.get('reference')
    references = reference if isinstance(reference, list) else [reference]
    if kind == 'ids':
        if not references or any(not (isinstance(item, str) and not _missing(item) or type(item) in (int, float) and 0 < item <= 9007199254740991 and int(item) == item) for item in references):
            raise ValueError('invalid external SSH key reference')
        return {'mode': 'external', 'ids': deepcopy(references), 'reference': references[0]}
    if len(references) != 1 or not isinstance(references[0], str) or _missing(references[0]):
        raise ValueError('one external SSH public key is required')
    if kind == 'fingerprint_file' and (not references[0].endswith('.pub') or any(c in references[0] for c in '\x00\r\n')):
        raise ValueError('external SSH key must name a regular .pub file')
    if opts.get('blue/event') == 'build' or opts.get('blue/dry-run') is True:
        if kind == 'fingerprint_file':
            value = ':'.join(['00'] * 16)
            return {'mode': 'external', 'ids': [value], 'reference': value}
        return {'mode': 'external', 'public_key': PLACEHOLDER}
    if kind == 'content':
        public = _public(references[0])
    elif kind in ('public_file', 'fingerprint_file'):
        name = references[0]
        env = os.environ if environment is None else environment
        if name.startswith('~/'):
            name = os.path.join(env.get('HOME') or str(Path.home()), name[2:])
        path = Path(name)
        if path.suffix != '.pub' or path.is_symlink() or not path.is_file():
            raise ValueError('external SSH key must name a regular .pub file')
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'r', encoding='utf-8') as stream:
                content = stream.read(65537)
            if len(content) > 65536:
                raise ValueError()
            public = _public(content)
        except (OSError, UnicodeError, ValueError):
            raise ValueError('invalid external SSH public key file') from None
    else:
        raise ValueError('unsupported external SSH key reference')
    if kind == 'fingerprint_file':
        value = ':'.join(f'{byte:02x}' for byte in hashlib.md5(base64.b64decode(public.split()[1])).digest())
        return {'mode': 'external', 'ids': [value], 'reference': value}
    return {'mode': 'external', 'public_key': public}
