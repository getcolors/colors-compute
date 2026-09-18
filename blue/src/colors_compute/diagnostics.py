"""Safe lifecycle diagnostics; never format exceptions, credentials or state."""
import os
from pathlib import Path
from .ssh import _mode

MESSAGES = {
    'missing-tool': ('Required infrastructure tools are missing from PATH.', 'Install the listed tools or load the deployment environment, then retry.'),
    'state-unreadable': ('Cannot read infrastructure state; absence has not been established.', 'Check backend access and credentials before retrying. Do not remove state or ownership records.'),
    'failed-operation-without-state': ('A previous infrastructure operation failed and its state file is missing. Cloud resources may still exist.', 'Inspect provider resources and reconcile the failed operation using the reviewed recovery procedure before retrying.'),
    'recorded-state-missing': ('The ownership journal records infrastructure whose state file is missing.', 'Inspect provider resources and recover the recorded state before retrying. Do not reset the journal.'),
}


class LifecycleDiagnostic(Exception):
    def __init__(self, code, tools=()):
        super().__init__(code)
        message, hint = MESSAGES[code]
        self.diagnostic = {'code': code, 'message': message, 'hint': hint}
        if code == 'missing-tool':
            self.diagnostic['tools'] = sorted(set(tools) & {'tofu', 'aws', 'gcloud', 'oci', 'ssh-keygen'})

    def result(self):
        d = self.diagnostic
        missing = ' Missing: ' + ', '.join(d['tools']) + '.' if d.get('tools') else ''
        return {'status': 'error', 'errors': [d['message'] + missing + ' Next: ' + d['hint']], 'diagnostics': [d]}


def required_tools(opts):
    tools = ['tofu']
    backend = opts.get('provider-backend', 'r2')
    tool = {'s3': 'aws', 'r2': 'aws', 'gcs': 'gcloud', 'oci': 'oci'}.get(backend)
    if tool:
        tools.append(tool)
    if opts.get('provider-compute') == 'oci':
        tools.append('oci')
    if _mode(opts)['mode'] == 'managed':
        tools.append('ssh-keygen')
    return sorted(set(tools))


def missing_tools(opts, environment):
    # Use exactly the supplied PATH, including empty entries, as execution does.
    paths = environment['PATH'].split(os.pathsep) if 'PATH' in environment else []
    return [tool for tool in required_tools(opts)
            if not any((p := Path(entry or '.') / tool).is_file() and os.access(p, os.X_OK) for entry in paths)]
