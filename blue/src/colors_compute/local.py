"""Filesystem state and conditional journal storage for the local backend."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024


def private_parents(path, root):
    """Create missing directories privately; reject symlinks and non-directories."""
    path, root = Path(path), Path(root)
    for directory in reversed((path, *path.parents)):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            if not stat.S_ISDIR(directory.lstat().st_mode):
                raise ValueError('invalid local state directory')
        if root in directory.parents:
            directory.chmod(0o700)


def _check_parents(path):
    for directory in reversed(Path(path).parents):
        try:
            mode = directory.lstat().st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(mode):
            raise ValueError('invalid local state directory')


def _open(path):
    _check_parents(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError('invalid local state file')
    return os.fdopen(descriptor, 'rb')


def presence(path):
    try:
        with _open(path) as stream:
            stream.read(1)
        return {'status': 'present'}
    except FileNotFoundError:
        return {'status': 'absent'}
    except Exception:
        return {'status': 'error'}


def protect_state(path):
    for filename in (str(path), str(path) + '.backup'):
        try:
            with _open(filename) as stream:
                os.fchmod(stream.fileno(), 0o600)
        except FileNotFoundError:
            pass


def _read(path):
    try:
        with _open(path) as stream:
            data = stream.read(MAX_DOCUMENT_BYTES + 1)
    except FileNotFoundError:
        return {'status': 'absent'}
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError('local journal exceeds size limit')
    def reject_constant(_):
        raise ValueError('invalid JSON')
    document = json.loads(data.decode('utf-8'), parse_constant=reject_constant)
    if not isinstance(document, dict):
        raise ValueError('invalid local journal')
    return {'status': 'present', 'document': document, 'etag': hashlib.sha256(data).hexdigest()}


def journal_session(filename, payload, intent, root):
    path = Path(filename)
    if payload is None:
        return _read(path)
    private_parents(path.parent, root)
    lock = Path(str(path) + '.lock')
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError:
        return {'status': 'conflict'}
    temporary = None
    try:
        observed = _read(path)
        condition = intent['condition']
        if ('if_none_match' in condition and observed['status'] != 'absent' or
                'if_match' in condition and (observed['status'] != 'present' or observed['etag'] != condition['if_match'])):
            return {'status': 'conflict'}
        descriptor, temporary = tempfile.mkstemp(prefix='.coordination-', dir=path.parent)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return {'status': 'written', 'etag': hashlib.sha256(payload).hexdigest()}
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
        lock.rmdir()
