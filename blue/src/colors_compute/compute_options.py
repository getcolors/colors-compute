"""Optional reviewed VM fields; omitted requirements preserve provider defaults."""
from importlib.resources import files
import json
from ._copy import deepcopy

def apply_options(provider, stage, request, documents, opts=None):
    descriptor = json.loads(files('colors_compute').joinpath('compute-options.json').read_text()).get(provider)
    request = deepcopy(request)
    option = (descriptor or {}).get('backups_option')
    if 'backups' not in request and option in (opts or {}):
        request['backups'] = {'enabled': opts[option]}
    selected = {key: request[key] for key in ('backups', 'ipv6') if key in request}
    if not selected:
        return documents
    if descriptor is None:
        raise ValueError('unsupported compute options capability')
    if 'ipv6' in selected and 'ipv6_field' not in descriptor:
        raise ValueError('unsupported compute options capability')
    if 'ipv6' in selected and type(selected['ipv6']) is not bool:
        raise ValueError('invalid compute IPv6 policy')
    backup = selected.get('backups')
    if 'backups' in selected:
        if not isinstance(backup, dict) or type(backup.get('enabled')) is not bool or set(backup) != ({'enabled', 'schedule'} if backup['enabled'] and 'schedule_field' in descriptor else {'enabled'}):
            raise ValueError('invalid compute backup policy')
        if backup['enabled'] and 'schedule_field' in descriptor:
            schedule = backup['schedule']
            if not isinstance(schedule, dict) or set(schedule) != {'type', 'hour'} or schedule['type'] != 'daily' or type(schedule['hour']) not in (int,float) or not 0 <= schedule['hour'] <= 23 or schedule['hour'] != int(schedule['hour']):
                raise ValueError('invalid compute backup schedule')
    if stage != 'node':
        return documents
    result = deepcopy(documents)
    resources = [doc.get('resource', {}).get(descriptor['resource_type'], {}).get(descriptor['resource_name']) for doc in result.values()]
    resources = [resource for resource in resources if resource is not None]
    if len(resources) != 1:
        raise ValueError('compute options resource unavailable')
    resource = resources[0]
    if 'ipv6' in selected:
        resource[descriptor['ipv6_field']] = selected['ipv6']
    if backup is not None:
        resource[descriptor['backups_field']] = descriptor['backups_values'][str(backup['enabled']).lower()]
        if backup['enabled'] and 'schedule_field' in descriptor:
            resource[descriptor['schedule_field']] = [{'type':'daily','hour':int(backup['schedule']['hour'])}]
    return result
