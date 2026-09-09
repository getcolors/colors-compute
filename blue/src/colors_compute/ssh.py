"""Shared local SSH lifecycle. Private key content never leaves the filesystem."""
import asyncio
import base64
from contextlib import contextmanager
from ._copy import deepcopy
import hashlib
import inspect
import os
from pathlib import Path
import re
import stat
import struct

from .backend import _run
from .contract import _missing, _safe, registry

PLACEHOLDER = 'ssh-ed25519 PLACEHOLDER managed-by-colors'


class SSHError(ValueError):
    pass


def _refuse(message):
    raise SSHError(message)


def _nonblank(value):
    return isinstance(value, str) and not _missing(value)


def _mode(opts):
    if not _safe(opts.get('profile')):
        _refuse(':profile must be a safe identifier')
    provider = opts.get('provider-compute')
    entry = registry()['compute'].get(provider) if isinstance(provider, str) else None
    if entry is None:
        _refuse('invalid SSH compute provider')
    settings = [name for name in [entry['ssh-setting'], *entry.get('ssh-aliases', {})] if name in opts]
    if len(settings) > 1:
        _refuse('ambiguous external SSH key settings')
    if not settings:
        return {'mode': 'managed'}
    setting = settings[0]
    value = opts[setting]
    if setting in entry.get('ssh-aliases', {}) and not _nonblank(value):
        _refuse('invalid external SSH key reference')
    if not (_nonblank(value) or isinstance(value, list) and value and all(
            _nonblank(v) or type(v) in (int, float) and 0 < v <= 9007199254740991 and v == int(v) for v in value)):
        _refuse('invalid external SSH key reference')
    result = {'mode': 'external', 'setting': setting, 'reference': deepcopy(value)}
    identity_setting = 'ssh-private-key-path' if 'ssh-private-key-path' in opts else provider + '-ssh-private-key'
    if identity_setting in opts:
        if not _nonblank(opts[identity_setting]):
            _refuse('invalid external SSH identity reference')
        result['private_key_path'] = opts[identity_setting]
    return result


def _planning(opts):
    return opts.get('blue/event') == 'build' or opts.get('blue/dry-run') is True


def _ownership(value):
    if value == {'status': 'fresh'}:
        return
    if (isinstance(value, dict) and set(value) == {'status', 'fingerprint'} and value['status'] == 'prepared'
            and isinstance(value['fingerprint'], str)
            and re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', value['fingerprint'])):
        return
    _refuse('SSH key ownership uncertain')


def _paths(opts, env):
    home = env.get('HOME') if _nonblank(env.get('HOME')) else str(Path.home())
    directory = Path(os.path.abspath(os.path.join(home, '.ssh')))
    name = opts['profile']
    return directory, directory / name, directory / (name + '.pub'), directory / (name + '.known_hosts')


def _exists(path):
    return os.path.lexists(path)


def _safe_paths(paths):
    for i, path in enumerate(paths):
        if path.is_symlink() or (_exists(path) and not (path.is_dir() if i == 0 else path.is_file())):
            _refuse('unsafe SSH key path')


@contextmanager
def _reservation(directory, profile):
    lock = directory / ('.' + profile + '.colors-key.lock')
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _refuse('SSH key operation reserved; explicit recovery required')
    try:
        yield
    finally:
        try:
            owned = os.fstat(fd)
            current = os.lstat(lock)
            if (owned.st_dev, owned.st_ino) != (current.st_dev, current.st_ino):
                _refuse('SSH key reservation changed; explicit recovery required')
            os.unlink(lock)
        finally:
            os.close(fd)


def _parts(public):
    text = public.strip()
    parts = text.split()
    if '\n' in text or '\r' in text or len(parts) < 2 or parts[0] != 'ssh-ed25519':
        _refuse('SSH keypair is inconsistent')
    return parts[:2]


def _fingerprint(public):
    blob = base64.b64decode(_parts(public)[1], validate=True)
    if len(blob) != 51 or blob[:15] != struct.pack('!I', 11) + b'ssh-ed25519' or blob[15:19] != struct.pack('!I', 32):
        _refuse('SSH keypair is inconsistent')
    return 'SHA256:' + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')


async def _call(fn, *args):
    value = fn(*args)
    return await value if inspect.isawaitable(value) else value


async def _acknowledge(callback, *args):
    try:
        if await _call(callback, *args) is not True:
            _refuse('SSH key ownership update failed')
    except asyncio.CancelledError:
        raise
    except Exception:
        _refuse('SSH key ownership update failed')


async def _verify(paths, env, runner, require_pair):
    directory, private, public, _ = paths
    if require_pair and not (private.exists() and public.exists()):
        _refuse('owned SSH keypair is missing')
    os.chmod(directory, 0o700)
    derived = None
    if private.exists():
        os.chmod(private, 0o600)
        result = await _call(runner, ['ssh-keygen', '-y', '-P', '', '-f', str(private)], str(directory), env, 30000)
        if result.exit != 0 or not isinstance(result.out, str):
            _refuse('SSH keypair is inconsistent')
        derived = result.out.strip()
    published = None
    if public.exists():
        os.chmod(public, 0o600)
        published = public.read_text().strip()
    if derived and published and _parts(derived) != _parts(published):
        _refuse('SSH keypair is inconsistent')
    value = published or derived
    return {'public_key': value, 'fingerprint': _fingerprint(value)}


async def prepare_keypair(opts, ownership, environment, record_intent, record_prepared, runner=None):
    try:
        selected = _mode(opts)
        if selected['mode'] == 'external':
            return selected
        if _planning(opts):
            path = '$HOME/.ssh/' + opts['profile']
            return {'mode': 'managed', 'private_key_path': path, 'public_key_path': path + '.pub',
                    'public_key': PLACEHOLDER, 'fingerprint': None}
        if 'blue/event' in opts and opts['blue/event'] != 'create':
            _refuse('SSH key preparation requires create')
        _ownership(ownership)
        env = dict(os.environ if environment is None else environment)
        paths = _paths(opts, env)
        _safe_paths(paths)
        directory, private, public, known = paths
        if ownership['status'] == 'fresh' and any(_exists(p) for p in paths[1:]):
            _refuse('unowned SSH key files exist; verify surviving hosts before recovery')
        if ownership['status'] == 'prepared' and not (private.exists() and public.exists()):
            _refuse('owned SSH keypair is missing')
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        with _reservation(directory, opts['profile']):
            _safe_paths(paths)
            if ownership['status'] == 'fresh':
                if any(_exists(p) for p in paths[1:]):
                    _refuse('unowned SSH key files exist; verify surviving hosts before recovery')
                await _acknowledge(record_intent)
                result = await _call(runner or _run, ['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C',
                    opts['profile'] + ' managed by Colors', '-f', str(private)], str(directory), env, 30000)
                if result.exit != 0:
                    _refuse('SSH key generation failed')
                verified = await _verify(paths, env, runner or _run, True)
                await _acknowledge(record_prepared, verified['fingerprint'])
            else:
                verified = await _verify(paths, env, runner or _run, True)
                if verified['fingerprint'] != ownership['fingerprint']:
                    _refuse('SSH key fingerprint differs from ownership')
            return {'mode': 'managed', 'private_key_path': str(private), 'public_key_path': str(public), **verified}
    except SSHError:
        raise
    except Exception:
        _refuse('SSH key operation failed')


async def cleanup_keypair(opts, ownership, authority, environment=None, runner=None):
    try:
        selected = _mode(opts)
        if selected['mode'] == 'external':
            return selected
        if _planning(opts):
            return {'mode': 'managed', 'cleaned': False, 'planned': True}
        if 'blue/event' in opts and opts['blue/event'] != 'delete':
            _refuse('SSH key cleanup requires delete')
        if (not isinstance(authority, dict) or set(authority) - {'all_resources_destroyed', 'known_hosts_owned'}
                or authority.get('all_resources_destroyed') is not True
                or 'known_hosts_owned' in authority and type(authority['known_hosts_owned']) is not bool):
            _refuse('SSH key cleanup requires complete resource destruction')
        _ownership(ownership)
        env = dict(os.environ if environment is None else environment)
        paths = _paths(opts, env)
        _safe_paths(paths)
        directory, private, public, known = paths
        if not directory.exists():
            return {'mode': 'managed', 'cleaned': True}
        os.chmod(directory, 0o700)
        with _reservation(directory, opts['profile']):
            _safe_paths(paths)
            if private.exists() or public.exists():
                if ownership['status'] != 'prepared':
                    _refuse('unowned SSH key files exist; verify surviving hosts before recovery')
                verified = await _verify(paths, env, runner or _run, False)
                if verified['fingerprint'] != ownership['fingerprint']:
                    _refuse('SSH key fingerprint differs from ownership')
                private.unlink(missing_ok=True)
                public.unlink(missing_ok=True)
            if authority.get('known_hosts_owned') is True and known.exists():
                if ownership['status'] != 'prepared':
                    _refuse('SSH known-host ownership uncertain')
                known.unlink()
            return {'mode': 'managed', 'cleaned': True}
    except SSHError:
        raise
    except Exception:
        _refuse('SSH key operation failed')
