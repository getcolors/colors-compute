import json

import pytest

from colors_compute.registration import registration_preflight


def body(provider, entries, following=None):
    data = {'ssh_keys': [{'id': identity, 'name': name, ('ssh_key' if provider == 'vultr' else 'public_key'): public} for identity, name, public in entries]}
    if provider == 'digitalocean':
        data['links'] = {'pages': {'next': following}} if following else {}
    else:
        data['meta'] = {'pagination': {'next_page': following}} if provider == 'hcloud' else {'links': {'next': following or ''}}
    return json.dumps(data).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,scope,token,next_value', [
    ('digitalocean', 'account', 'COLORS_PAR_DO_TOKEN', 'https://api.digitalocean.com/v2/account/keys?page=2'),
    ('hcloud', 'project', 'COLORS_PAR_HCLOUD_TOKEN', 2),
    ('vultr', 'account', 'COLORS_PAR_VULTR_API_KEY', 'next/cursor&safe')])
async def test_paginated_owned_registration(provider, scope, token, next_value):
    calls = []
    async def http(url, headers):
        calls.append(url)
        assert headers['Authorization'] == 'Bearer secret-fixture'
        return body(provider, [(1, 'other', 'ssh-ed25519 other')], next_value) if len(calls) == 1 else body(provider, [(2, 'demo', 'ssh-ed25519 mine')])
    assert await registration_preflight({'profile': 'demo', 'provider-compute': provider}, 'managed', {'provider': provider, 'scope': scope, 'id': 2}, 'ssh-ed25519 mine comment', {token: 'secret-fixture'}, http) == {'status': 'checked'}
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('next_value', ['https://evil.invalid/keys', 'http://api.digitalocean.com/v2/account/keys', 'https://api.digitalocean.com/v2/account/keys#fragment', 'https://api.digitalocean.com/v2/droplets?page=2', 'https://api.digitalocean.com/v2/account/keys?token=bad'])
async def test_pagination_cannot_forward_credentials(next_value):
    calls = []
    async def http(url, headers):
        calls.append(url)
        return body('digitalocean', [], next_value)
    with pytest.raises(ValueError, match='preflight failed'):
        await registration_preflight({'profile': 'demo', 'provider-compute': 'digitalocean'}, 'managed', environment={'COLORS_PAR_DO_TOKEN': 'secret'}, http=http)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [b'{}', b'{"ssh_keys":null}', b'\xff', b'\xef\xbb\xbf{}', b'{"ssh_keys":[],"meta":{}}', b'{"ssh_keys":[{}]}'])
async def test_malformed_response_closed(payload):
    with pytest.raises(ValueError, match='preflight failed'):
        await registration_preflight({'profile': 'demo', 'provider-compute': 'vultr'}, 'managed', environment={'COLORS_PAR_VULTR_API_KEY': 'secret'}, http=lambda *args: payload)


@pytest.mark.asyncio
@pytest.mark.parametrize('public,message', [(None, 'foreign SSH registration'), ('ssh-ed25519 mine', 'unowned SSH registration')])
async def test_collision_never_adopts(public, message):
    with pytest.raises(ValueError, match=message):
        await registration_preflight({'profile': 'demo', 'provider-compute': 'vultr'}, 'managed', public_key=public, environment={'COLORS_PAR_VULTR_API_KEY': 'secret'}, http=lambda *args: body('vultr', [('foreign', 'demo', 'ssh-ed25519 mine')]))


@pytest.mark.asyncio
async def test_external_and_unique_aws_gate_skip_network():
    def http(*args):
        raise AssertionError('must not call')
    for provider, mode in [('vultr', 'external'), ('aws', 'managed')]:
        assert await registration_preflight({'profile': 'demo', 'provider-compute': provider}, mode, http=http) == {'status': 'skipped'}
