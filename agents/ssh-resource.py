#!/usr/bin/env python3
"""Private OpenSSH/conditional-storage adapter. Protocol output never contains keys."""
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import pty
import termios
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid


class ResourceError(Exception):
    pass


def require(ok, message):
    if not ok:
        raise ResourceError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def private_directory(path):
    path = Path(path)
    require(path.is_absolute(), 'absolute directory required')
    for part in reversed((path, *path.parents)):
        try:
            part.mkdir(mode=0o700)
        except FileExistsError:
            require(stat.S_ISDIR(part.lstat().st_mode), 'unsafe SSH directory')
    require(path.stat().st_uid == os.getuid(), 'SSH directory ownership mismatch')
    path.chmod(0o700)
    return path


def read_private(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise ResourceError('unsafe or unreadable SSH file') from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid(), 'unsafe SSH file')
        require(info.st_mode & 0o077 == 0, 'SSH file permissions are not private')
        return stream.read(1024 * 1024 + 1)


def atomic_write(path, data):
    path = Path(path)
    if path.exists() or path.is_symlink():
        read_private(path)
    fd, temporary = tempfile.mkstemp(prefix='.publish-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def clean_environment():
    # Explicit allowlist: provider credentials and secret bindings cannot leak to SSH.
    return {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'SYSTEMROOT') if k in os.environ} | {'LC_ALL': 'C'}


def _run(args, env=None, timeout=120, allowed=(0,)):
    try:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env or clean_environment(),
                                   start_new_session=True, umask=0o077)
        try:
            out, err = process.communicate(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.stdout.close()
                process.stderr.close()
            raise
    except (OSError, subprocess.TimeoutExpired):
        raise ResourceError('SSH resource command unavailable or timed out') from None
    if process.returncode not in allowed:
        raise ResourceError('SSH resource command failed')
    return process.returncode, out, err


def run(args, env=None, timeout=120, allowed=(0,)):
    if env and env.get('COLORS_SSH_ASKPASS') == '1':
        with tempfile.TemporaryDirectory(prefix='colors-askpass-') as tmp:
            controlled = dict(env, COLORS_SSH_ASKPASS_COUNTER=str(Path(tmp) / 'attempt'))
            return _run(args, controlled, timeout, allowed)
    return _run(args, env, timeout, allowed)


def keygen(args, passphrase, new_passphrase=None):
    """Controlled TTY prompts cannot fall back to an empty askpass response.

    Never close the terminal while the child is alive: an EOF may otherwise be
    interpreted as an empty passphrase by some OpenSSH keygen versions.
    """
    master, slave = pty.openpty()
    def session():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    process = None
    try:
        process = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave,
                                   env=clean_environment(), preexec_fn=session, umask=0o077)
        os.close(slave)
        slave = None
        pending = b''
        answers = 0
        expected = 3 if new_passphrase is not None else 2
        deadline = time.monotonic() + 120
        while process.poll() is None:
            require(time.monotonic() < deadline, 'SSH key generation timed out')
            readable, _, _ = select.select([master], [], [], .05)
            if not readable:
                continue
            try:
                chunk = os.read(master, 8192)
            except OSError:
                break
            if not chunk:
                break
            pending += chunk
            lower = pending.lower()
            if b'passphrase' in lower and pending.rstrip().endswith(b':'):
                require(answers < expected, 'unexpected SSH passphrase prompt')
                require(not termios.tcgetattr(master)[3] & termios.ECHO, 'unsafe SSH prompt echo')
                if new_passphrase is not None:
                    require((answers == 0) == (b'old passphrase' in lower), 'unexpected SSH rotation prompt')
                    answer = passphrase if answers == 0 else new_passphrase
                else:
                    answer = passphrase
                os.write(master, answer.encode() + b'\n')
                answers += 1
                pending = b''
            require(len(pending) < 65536, 'unexpected SSH key generation output')
        require(process.wait(timeout=5) == 0 and answers == expected, 'SSH key generation failed')
    finally:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        os.close(master)
        if slave is not None:
            os.close(slave)


def secret(binding):
    require(isinstance(binding, str) and re.fullmatch(r'COLORS_PAR_[A-Z][A-Z0-9_]*', binding), 'invalid passphrase binding')
    value = os.environ.get(binding)
    require(isinstance(value, str) and bool(value) and len(value.encode()) <= 1000
            and not any(ord(c) < 32 or ord(c) == 127 for c in value), 'missing or invalid SSH passphrase')
    return value


def askpass_environment(passphrase, new_passphrase=None, socket=None):
    env = clean_environment() | {'SSH_ASKPASS': str(Path(__file__).absolute()),
        'SSH_ASKPASS_REQUIRE': 'force', 'DISPLAY': 'colors:0',
        'COLORS_SSH_PASSPHRASE': passphrase, 'COLORS_SSH_ASKPASS': '1'}
    if new_passphrase is not None:
        env['COLORS_SSH_NEW_PASSPHRASE'] = new_passphrase
    if socket:
        env['SSH_AUTH_SOCK'] = socket
    return env


def fingerprint(public):
    require(isinstance(public, str), 'invalid SSH public identity')
    parts = public.strip().split()
    require(len(parts) == 2 and parts[0] == 'ssh-ed25519', 'invalid ED25519 public identity')
    try:
        wire = base64.b64decode(parts[1], validate=True)
    except ValueError:
        raise ResourceError('invalid SSH public identity') from None
    require(len(wire) == 51 and wire[:19] == struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32), 'invalid ED25519 public identity')
    return 'SHA256:' + base64.b64encode(hashlib.sha256(wire).digest()).decode().rstrip('=')


def encrypted_public(ciphertext):
    try:
        lines = ciphertext.strip().splitlines()
        require(lines[0] == '-----BEGIN OPENSSH PRIVATE KEY-----' and lines[-1] == '-----END OPENSSH PRIVATE KEY-----', 'invalid encrypted key')
        raw = base64.b64decode(''.join(lines[1:-1]), validate=True)
        require(raw.startswith(b'openssh-key-v1\0'), 'invalid encrypted key')
        offset = 15
        def field():
            nonlocal offset
            size = struct.unpack('>I', raw[offset:offset+4])[0]
            offset += 4
            value = raw[offset:offset+size]
            require(len(value) == size, 'invalid encrypted key')
            offset += size
            return value
        cipher, kdf, options = field(), field(), field()
        require(cipher == b'aes256-ctr' and kdf == b'bcrypt', 'unencrypted or unsupported private key refused')
        salt_length = struct.unpack('>I', options[:4])[0]
        rounds = struct.unpack('>I', options[4+salt_length:8+salt_length])[0]
        require(rounds == 64, 'unexpected SSH KDF policy')
        require(struct.unpack('>I', raw[offset:offset+4])[0] == 1, 'invalid encrypted key count')
        offset += 4
        public = 'ssh-ed25519 ' + base64.b64encode(field()).decode()
        fingerprint(public)
        require(bool(field()) and offset == len(raw), 'invalid encrypted key payload')
        return public
    except (ValueError, IndexError, struct.error, TypeError):
        raise ResourceError('invalid encrypted SSH key') from None


def public_cache(plan):
    # Backend overrides can share a workstation directory but never a cache file.
    digest = hashlib.sha256(plan['reference'].encode()).hexdigest()
    return Path(plan['directory']) / ('identity-' + digest + '.pub')


def validate_record(record, plan):
    require(isinstance(record, dict) and record.get('version') == 2 and record.get('reference') == plan['reference'], 'SSH authority identity mismatch')
    require(record.get('status') in ('ready', 'locked', 'deleted'), 'invalid SSH authority status')
    if record.get('encrypted_key') is not None:
        public = encrypted_public(record['encrypted_key'])
        require(public == record.get('public_key') and fingerprint(public) == record.get('fingerprint'), 'SSH authority fingerprint mismatch')
    elif record['status'] == 'ready':
        raise ResourceError('encrypted SSH authority missing; recover explicitly')
    return record


def result(record):
    return {k: record[k] for k in ('reference', 'public_key', 'fingerprint')} | {'status': 'ready'}


def storage_environment(plan):
    env = clean_environment() | {'AWS_PAGER': '', 'AWS_CLI_AUTO_PROMPT': 'off',
        'AWS_MAX_ATTEMPTS': '1', 'AWS_REQUEST_CHECKSUM_CALCULATION': 'when_required',
        'AWS_RESPONSE_CHECKSUM_VALIDATION': 'when_required'}
    for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_PROFILE',
                 'AWS_DEFAULT_PROFILE', 'AWS_CONFIG_FILE', 'AWS_SHARED_CREDENTIALS_FILE', 'AWS_CA_BUNDLE',
                 'GOOGLE_APPLICATION_CREDENTIALS', 'CLOUDSDK_CONFIG'):
        if name in os.environ:
            env[name] = os.environ[name]
    prefix = plan['storage'].get('credential_prefix')
    if prefix:
        for name in list(env):
            if name.startswith('AWS_') and name != 'AWS_CA_BUNDLE':
                del env[name]
        for suffix in ('ACCESS_KEY_ID', 'SECRET_ACCESS_KEY'):
            value = os.environ.get('COLORS_PAR_' + prefix + '_' + suffix)
            require(bool(value), 'missing SSH backend credentials')
            env['AWS_' + suffix] = value
    return env


class Store:
    def __init__(self, plan):
        self.plan = plan
        self.directory = private_directory(plan['directory'])
        self.storage = plan['storage']
        self.local = self.storage['kind'] == 'local'
        self.env = storage_environment(plan)

    @contextlib.contextmanager
    def local_lock(self):
        if not self.local:
            yield
            return
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(self.directory / '.lock', flags | os.O_EXCL, 0o600)
            self.first_local_lock = True
        except FileExistsError:
            fd = os.open(self.directory / '.lock', flags, 0o600)
            self.first_local_lock = False
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid(), 'unsafe SSH lock')
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ResourceError('SSH resource busy') from None
            yield
        finally:
            os.close(fd)

    def aws(self, verb, arguments):
        args = ['aws', 's3api', verb, '--bucket', self.storage['bucket'], '--key', self.plan['object_key'],
                '--region', self.storage['region'], '--no-cli-pager', '--output', 'json'] + arguments
        if self.storage.get('endpoint'):
            args += ['--endpoint-url', self.storage['endpoint']]
        return run(args, self.env, allowed=tuple(range(256)))

    def read(self):
        if self.local:
            raw = read_private(self.directory / 'resource.json')
            version = hashlib.sha256(raw).hexdigest() if raw is not None else None
        else:
            with tempfile.TemporaryDirectory(prefix='.read-', dir=self.directory) as tmp:
                path = Path(tmp) / 'record'
                if self.storage['kind'] == 'gcs':
                    url = 'gs://' + self.storage['bucket'] + '/' + self.plan['object_key']
                    code, out, err = run(['gcloud', 'storage', 'objects', 'describe', url, '--format=json'], self.env, allowed=tuple(range(256)))
                    if code:
                        # A bucket/access failure must never be treated as object absence.
                        require(b'No URLs matched' in err or b'HTTPError 404' in err, 'SSH backend read failed')
                        run(['gcloud', 'storage', 'buckets', 'describe', 'gs://' + self.storage['bucket'], '--format=json'], self.env)
                        return None, None
                    generation = str(json.loads(out)['generation'])
                    run(['gcloud', 'storage', 'cp', url + '#' + generation, str(path)], self.env)
                    version = generation
                else:
                    code, out, err = self.aws('get-object', [str(path)])
                    if code:
                        if re.search(rb'An error occurred \(NoSuchKey\) when calling the GetObject operation', err):
                            return None, None
                        raise ResourceError('SSH backend read failed')
                    version = json.loads(out)['ETag']
                path.chmod(0o600)
                raw = read_private(path)
        if raw is None:
            return None, None
        require(len(raw) <= 1024 * 1024, 'SSH authority oversized')
        try:
            return validate_record(json.loads(raw), self.plan), version
        except (ValueError, KeyError, TypeError):
            raise ResourceError('invalid SSH authority') from None

    def write(self, record, version):
        data = canonical(record).encode()
        if self.local:
            _, current = self.read()
            require(current == version, 'SSH resource changed concurrently')
            atomic_write(self.directory / 'resource.json', data)
            return hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory(prefix='.write-', dir=self.directory) as tmp:
            path = Path(tmp) / 'record'
            atomic_write(path, data)
            if self.storage['kind'] == 'gcs':
                url = 'gs://' + self.storage['bucket'] + '/' + self.plan['object_key']
                run(['gcloud', 'storage', 'cp', str(path), url, '--if-generation-match=' + (version or '0')], self.env)
                # The current record must still be ours; concurrent writers cannot
                # acquire it while its lock token is present.
                observed, updated = self.read()
                require(observed == record, 'SSH conditional write could not be verified')
                return updated
            args = ['--body', str(path), '--content-type', 'application/json']
            args += ['--if-match', version] if version else ['--if-none-match', '*']
            code, out, _ = self.aws('put-object', args)
            require(code == 0, 'SSH conditional write failed or uncertain; inspect authority before retry')
            return json.loads(out)['ETag']


@contextlib.contextmanager
def locked(plan, expected=None, recover_token=None, allow_absent=False):
    store = Store(plan)
    with store.local_lock():
        record, version = store.read()
        if record is None:
            require(not store.local or store.first_local_lock, 'SSH authority missing beside existing resource lock; recover explicitly')
            require(not public_cache(plan).exists(), 'SSH authority missing beside known public identity; recover explicitly')
        require(record is not None or allow_absent, 'SSH authority missing; recover explicitly')
        if expected is not None:
            require(record is not None, 'SSH authority missing; recover explicitly')
            require(all(record.get(k) == expected.get(k) for k in ('reference', 'public_key', 'fingerprint')), 'SSH expected identity mismatch')
        if record and record['status'] == 'locked':
            require(recover_token and record.get('lock_token') == recover_token, 'SSH resource busy or interrupted; explicit recovery required')
        elif recover_token:
            raise ResourceError('SSH resource is not interrupted')
        require(not record or record['status'] != 'deleted', 'SSH resource was deleted; name reuse refused')
        previous = dict(record) if record else None
        record = dict(record or {'version': 2, 'reference': plan['reference']})
        record.update(status='locked', lock_token=uuid.uuid4().hex)
        version = store.write(record, version)
        # No automatic finally-unlock: uncertain operations retain their lock.
        yield store, record, version, previous


def publish(store, record, version, status='ready'):
    final = {k: v for k, v in record.items() if k != 'lock_token'} | {'status': status}
    store.write(final, version)
    return final


def verify_unlock(record, binding, directory):
    with tempfile.TemporaryDirectory(prefix='.unlock-', dir=directory) as tmp:
        path = Path(tmp) / 'key'
        atomic_write(path, record['encrypted_key'].encode())
        _, out, _ = run(['ssh-keygen', '-y', '-f', str(path)], askpass_environment(secret(binding)))
        public = ' '.join(out.decode().strip().split()[:2])
        require(public == record['public_key'], 'SSH unlocked identity mismatch')


def resource(plan, request, operation):
    require(operation in ('create', 'inspect', 'rotate', 'delete', 'recover'), 'invalid SSH operation')
    expected = request.get('expected')
    if operation == 'inspect':
        store = Store(plan)
        record, _ = store.read()
        require(record is not None, 'SSH authority missing')
        require(record['status'] == 'ready', 'SSH resource not ready; explicit recovery required')
        if expected:
            require(all(record.get(k) == expected.get(k) for k in ('reference', 'public_key', 'fingerprint')), 'SSH expected identity mismatch')
        return result(record)
    if operation in ('create', 'rotate', 'recover'):
        secret(request['passphrase_env'])
    if operation == 'rotate':
        secret(request.get('new_passphrase_env'))
    if operation == 'delete':
        require(request.get('allow_delete') is True and request.get('consumers_destroyed') is True, 'SSH deletion requires explicit consumer teardown confirmation')
        store = Store(plan)
        with store.local_lock():
            prior, _ = store.read()
            if prior and prior['status'] == 'deleted':
                public_cache(plan).unlink(missing_ok=True)
                return {'status': 'destroyed', 'reference': plan['reference']}
    with locked(plan, expected, request.get('lock_token') if operation == 'recover' else None, operation == 'create') as (store, record, version, previous):
        if operation != 'create':
            require(previous is not None and record.get('encrypted_key'), 'SSH authority missing; recover explicitly')
        if operation == 'delete':
            final = {'version': 2, 'reference': plan['reference'], 'status': 'deleted'}
            store.write(final, version)
            public_cache(plan).unlink(missing_ok=True)
            return {'status': 'destroyed', 'reference': plan['reference']}
        try:
            if not record.get('encrypted_key'):
                require(operation == 'create' and previous is None, 'SSH authority missing; recover explicitly')
                with tempfile.TemporaryDirectory(prefix='.generate-', dir=store.directory) as tmp:
                    path = Path(tmp) / 'key'
                    keygen(['ssh-keygen', '-q', '-t', 'ed25519', '-a', '64', '-Z', 'aes256-ctr', '-C', '', '-f', str(path)], secret(request['passphrase_env']))
                    ciphertext = read_private(path).decode()
                    public = encrypted_public(ciphertext)
                    record.update(encrypted_key=ciphertext, public_key=public, fingerprint=fingerprint(public), kdf_rounds=64)
                    # Persist the bundle while still locked, before reporting ready.
                    version = store.write(record, version)
            elif operation in ('rotate', 'recover'):
                verify_unlock(record, request['passphrase_env'], store.directory)
            if operation == 'rotate':
                with tempfile.TemporaryDirectory(prefix='.rotate-', dir=store.directory) as tmp:
                    path = Path(tmp) / 'key'
                    atomic_write(path, record['encrypted_key'].encode())
                    keygen(['ssh-keygen', '-p', '-a', '64', '-Z', 'aes256-ctr', '-f', str(path)],
                        secret(request['passphrase_env']), secret(request['new_passphrase_env']))
                    ciphertext = read_private(path).decode()
                    require(encrypted_public(ciphertext) == record['public_key'], 'rotation changed SSH identity')
                    record['encrypted_key'] = ciphertext
        except BaseException:
            # A failed unlock/generation did not mutate authority beyond our lock.
            # Restore an existing ready version, but retain a new reservation.
            if previous and previous.get('status') == 'ready':
                store.write(previous, version)
            raise
        final = publish(store, record, version)
        atomic_write(public_cache(plan), (final['public_key'] + '\n').encode())
        return result(final)


@contextlib.contextmanager
def agent_session(entries, lifetime=900):
    require(type(lifetime) is int and 1 <= lifetime <= 3600, 'invalid agent lifetime')
    require(isinstance(entries, list) and bool(entries), 'agent requires SSH resources')
    with tempfile.TemporaryDirectory(prefix='colors-agent-', dir='/tmp') as tmp:
        os.chmod(tmp, 0o700)
        socket = str(Path(tmp) / 'agent.sock')
        agent = subprocess.Popen(['ssh-agent', '-D', '-a', socket, '-t', str(lifetime)], env=clean_environment(),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, umask=0o077)
        try:
            deadline = time.monotonic() + 10
            while not Path(socket).exists():
                require(agent.poll() is None and time.monotonic() < deadline, 'SSH agent failed to start')
                time.sleep(.01)
            identities = {}
            def load():
                expected_fingerprints = set()
                for index, entry in enumerate(entries):
                    plan, request, expected = entry['plan'], entry['request'], entry['resource']
                    passphrase = secret(request['passphrase_env'])
                    with locked(plan, expected) as (store, record, version, previous):
                        try:
                            key = Path(tmp) / ('encrypted-' + str(index))
                            atomic_write(key, record['encrypted_key'].encode())
                            run(['ssh-add', '-t', str(lifetime), str(key)], askpass_environment(passphrase, socket=socket))
                            key.unlink()
                            public_path = public_cache(plan)
                            atomic_write(public_path, (record['public_key'] + '\n').encode())
                            identities[record['reference']] = str(public_path)
                            expected_fingerprints.add(record['fingerprint'])
                        except BaseException as error:
                            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                                # Stop authority immediately; do not block cancellation
                                # on remote lock release. Recovery retains its token.
                                agent.terminate()
                                raise
                            publish(store, record, version)
                            raise
                        publish(store, record, version)
                _, output, _ = run(['ssh-add', '-l', '-E', 'sha256'], clean_environment() | {'SSH_AUTH_SOCK': socket})
                observed = {line.split()[1] for line in output.decode().splitlines()}
                require(observed == expected_fingerprints, 'agent loaded identities mismatch')
            load()
            yield ({'status': 'ready', 'socket': socket, 'identities': identities,
                    'environment': {'SSH_AUTH_SOCK': socket}}, load)
        finally:
            agent.terminate()
            try:
                agent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                agent.kill()
                agent.wait()


def main():
    if os.environ.get('COLORS_SSH_ASKPASS') == '1':
        counter = Path(os.environ['COLORS_SSH_ASKPASS_COUNTER'])
        if counter.exists():
            sys.exit(1)
        fd = os.open(counter, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        prompt = sys.argv[1] if len(sys.argv) > 1 else ''
        binding = 'COLORS_SSH_PASSPHRASE'
        if 'COLORS_SSH_NEW_PASSPHRASE' in os.environ and 'old passphrase' not in prompt.lower():
            binding = 'COLORS_SSH_NEW_PASSPHRASE'
        sys.stdout.write(os.environ[binding] + '\n')
        return
    def interrupted(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        message = json.loads(sys.stdin.readline())
        if message['operation'] == 'agent':
            with agent_session(message['resources'], message.get('lifetime', 900)) as (ready, renew):
                print(canonical(ready), flush=True)
                # Renew only while the scope's pipe remains open. EOF closes it.
                interval = max(0.25, message.get('lifetime', 900) / 2)
                while True:
                    readable, _, _ = select.select([sys.stdin], [], [], interval)
                    if readable:
                        if not os.read(sys.stdin.fileno(), 1):
                            break
                    else:
                        renew()
        else:
            print(canonical(resource(message['plan'], message['request'], message['operation'])), flush=True)
    except (Exception, KeyboardInterrupt) as error:
        # Deliberately omit raw exception/provider/SSH diagnostics and all inputs.
        print(canonical({'status': 'error', 'error': {'code': 'ssh_resource_failed',
            'message': str(error) if isinstance(error, ResourceError) else 'SSH resource operation failed; inspect authority and credential bindings before retry'}}), flush=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
