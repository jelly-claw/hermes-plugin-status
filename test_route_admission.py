"""Admission-level regression: a Status group message stamped with a profile is refused by the
gateway (RuntimeStoreError profile_mismatch) unless profile_routes route that chat to the profile.
Drives gateway.session_ingress_context.capture_provenance exactly like the live adapter path,
mirroring tests/gateway/test_ingress_binding_user_id_route.py.

Run (no pytest in the hermes venv; hermes_bootstrap must be the first Hermes import so the
isolated runtime is activated):
    cd ~/.hermes/hermes-agent && venv/bin/python ~/.hermes/plugins/status/test_route_admission.py
"""
import asyncio, os, sys, tempfile, weakref
from pathlib import Path

sys.path.insert(0, os.path.expanduser(os.environ.get("HERMES_AGENT_DIR", "~/.hermes/hermes-agent")))
import hermes_bootstrap  # noqa: F401  entry points import this first: activates the isolated runtime
# Import under the real data root first (the isolated-runtime bootstrap keys deps on HERMES_HOME),
# then redirect the home exactly as the upstream pytest fixture does.
import hermes_state
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.profile_routing import parse_profile_routes
from gateway.run import GatewayRunner
from gateway.run_runtime import initialize_gateway_runtime
from gateway.runtime_ownership import process_ownership
from gateway.session import SessionSource
from gateway.session_ingress_context import capture_provenance, native_callback, register_transport_home
from hermes_state_runtime import RuntimeStoreError
import gateway.platform_registry as pr
from gateway.platform_registry import PlatformEntry

tmp = Path(tempfile.mkdtemp(prefix='hermes-repro-'))
root = tmp / '.hermes'
root.mkdir(mode=0o700)
os.environ['HERMES_HOME'] = str(root)
Path.home = classmethod(lambda cls: tmp)
hermes_state.DEFAULT_DB_PATH = hermes_state._IMPORT_DEFAULT_DB_PATH
reg = getattr(pr, 'platform_registry', None) or getattr(pr, 'registry')
reg.register(PlatformEntry(name='status', label='Status', adapter_factory=lambda c: None, check_fn=lambda: True))

PROFILE = 'example-group'
GROUP_ID = ('11111111-2222-4333-8444-555555555555-0x04aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
            'ccccccccccccccccc')
SENDER = '0x04' + 'a' * 128


class _FakeAdapter:
    pass


async def admit(routes, prestamp):
    homes = [('default', root)]
    phome = root / 'profiles' / PROFILE
    if not phome.is_dir():
        phome.mkdir(parents=True, mode=0o700)
        (phome / 'config.yaml').write_text('{}')
    homes.append((PROFILE, phome))
    config = GatewayConfig()
    config.multiplex_profiles = True
    config._runtime_profile_homes = tuple(homes)
    config.profile_routes = parse_profile_routes(routes)
    runner = GatewayRunner(config)
    process_ownership.reserve([h for _, h in homes])
    try:
        await initialize_gateway_runtime(runner)
        register_transport_home(runner, None, root)
        adapter = _FakeAdapter()
        runner.adapters = {Platform('status'): adapter}
        runner._is_user_authorized_for_source = lambda *a, **k: True
        source = SessionSource(platform=Platform('status'), chat_id=GROUP_ID, chat_type='group',
                               user_id=SENDER, user_name='alice')
        routed = runner._profile_name_for_source(source)
        source.profile = prestamp if prestamp is not None else routed
        source._transport_adapter_ref = weakref.ref(adapter)
        event = MessageEvent(text='you latest housing report fail', message_type=MessageType.TEXT,
                             source=source, message_id='m1')
        with native_callback(runner, event, root, None):
            prov = capture_provenance(runner, event)
        return routed, prov
    finally:
        for authority in list(getattr(runner, 'session_authorities', []) or []):
            authority.db.close()
        for _, h in homes:
            process_ownership.release(h)


async def main():
    route = [{'name': 'housing', 'platform': 'status', 'chat_id': GROUP_ID, 'profile': PROFILE}]
    # A: no routes, plugin pre-stamps the profile (today's production state)
    try:
        await admit([], PROFILE)
        print('A: UNEXPECTED success (no routes, pre-stamped)')
    except RuntimeStoreError as e:
        print(f'A: no routes + pre-stamp -> RuntimeStoreError({e})  [reproduces production]')
    # B: route present, plugin pre-stamps the same profile
    routed, prov = await admit(route, PROFILE)
    print(f'B: route + pre-stamp -> routed={routed} runtime_home={prov["runtime_home"]}')
    # C: route present, no plugin pre-stamp (build_source stamping only)
    routed, prov = await admit(route, None)
    print(f'C: route only       -> routed={routed} runtime_home={prov["runtime_home"]}')
    # D: route present but plugin pre-stamps a DIFFERENT profile (map/route disagreement)
    try:
        await admit(route, 'group-test')
        print('D: UNEXPECTED success (route/prestamp disagree)')
    except RuntimeStoreError as e:
        print(f'D: route + different pre-stamp -> RuntimeStoreError({e})')

asyncio.run(main())
