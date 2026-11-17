"""profile_routes bookkeeping for Status group agents (group_agents.py).

Regression for the 2026-11 Hermes update: admission (gateway/session_ingress_context.py::_binding)
refuses a message stamped with a profile that the gateway's own profile_routes do not route the
chat to ('profile_mismatch'), so the map must be mirrored into config.yaml and the live table.

Run (no pytest in the hermes venv):
    cd ~/.hermes/hermes-agent && venv/bin/python ~/.hermes/plugins/status/test_profile_routes.py
"""
import os
import pathlib
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.expanduser(os.environ.get("HERMES_AGENT_DIR", "~/.hermes/hermes-agent")))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from status import group_agents as ga  # noqa: E402
from gateway.profile_routing import ProfileRoute  # noqa: E402

CHAT = ("11111111-2222-4333-8444-555555555555-0x04aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        "ccccccccccccccccc")
OTHER = "99999999-8888-4777-8666-555555555555-0x04deadbeef"
PROFILE = "example-group"


def _fake_store(initial):
    state = {"cfg": {"profile_routes": list(initial)}, "saves": 0}

    def load():
        return {"model": {"default": "x"}, **state["cfg"]}

    def save(cfg, **kw):
        state["saves"] += 1
        state["cfg"] = {"profile_routes": cfg.get("profile_routes") or []}
    return state, load, save


def _with_profile_dirs(existing):
    tmp = pathlib.Path(tempfile.mkdtemp())
    for name in existing:
        (tmp / name).mkdir()
    ga._profile_dir = lambda name: tmp / name
    return tmp


def test_is_chat_route_dict_and_parsed():
    d = {"name": "x", "platform": "status", "chat_id": CHAT, "profile": PROFILE}
    assert ga._is_chat_route(d, CHAT)
    assert not ga._is_chat_route(d, OTHER)
    assert not ga._is_chat_route({**d, "platform": "telegram"}, CHAT)
    assert not ga._is_chat_route({**d, "user_id": "0x04"}, CHAT)
    assert not ga._is_chat_route({**d, "thread_id": "t"}, CHAT)
    assert not ga._is_chat_route({**d, "bot_profile": "other"}, CHAT)
    assert not ga._is_chat_route({**d, "enabled": False}, CHAT)
    assert ga._is_chat_route(ProfileRoute(name="x", platform="status", profile=PROFILE, chat_id=CHAT), CHAT)


def test_persisted_route_add_is_idempotent():
    _with_profile_dirs([PROFILE])
    state, load, save = _fake_store([])
    assert ga.ensure_persisted_route(CHAT, PROFILE, _load=load, _save=save)
    assert state["saves"] == 1
    assert ga.persisted_route_profile(CHAT, state["cfg"]["profile_routes"]) == PROFILE
    assert ga.ensure_persisted_route(CHAT, PROFILE, _load=load, _save=save)
    assert state["saves"] == 1, "no rewrite when the route is already there"
    route = state["cfg"]["profile_routes"][0]
    assert route == {"name": "status-group:" + PROFILE, "platform": "status", "chat_id": CHAT, "profile": PROFILE}


def test_persisted_route_reassign_and_remove_keep_other_routes():
    _with_profile_dirs([PROFILE, "group-test"])
    foreign = {"name": "tg", "platform": "telegram", "chat_id": "-100", "profile": "tg"}
    other = {"name": "o", "platform": "status", "chat_id": OTHER, "profile": "group-test"}
    state, load, save = _fake_store([foreign, other, {"name": "old", "platform": "status", "chat_id": CHAT, "profile": "group-test"}])
    assert ga.ensure_persisted_route(CHAT, PROFILE, _load=load, _save=save)
    routes = state["cfg"]["profile_routes"]
    assert foreign in routes and other in routes
    assert ga.persisted_route_profile(CHAT, routes) == PROFILE
    assert len(routes) == 3
    # opt-out = no route for the chat, everything else untouched
    assert ga.ensure_persisted_route(CHAT, None, _load=load, _save=save)
    routes = state["cfg"]["profile_routes"]
    assert routes == [foreign, other]
    assert ga.persisted_route_profile(CHAT, routes) is None


def test_persisted_route_refuses_missing_profile_dir():
    # A route to a profile with no directory would make the gateway drop the chat at next boot.
    _with_profile_dirs([])
    state, load, save = _fake_store([])
    assert not ga.ensure_persisted_route(CHAT, PROFILE, _load=load, _save=save)
    assert state["saves"] == 0


def test_sync_persisted_routes_writes_once_and_skips_missing_dirs():
    _with_profile_dirs([PROFILE])
    state, load, save = _fake_store([])
    done = ga.sync_persisted_routes({CHAT: PROFILE, OTHER: "not-created-yet"}, _load=load, _save=save)
    assert done == {CHAT: True, OTHER: False}
    assert state["saves"] == 1
    routes = state["cfg"]["profile_routes"]
    assert [r["profile"] for r in routes] == [PROFILE]
    assert ga.sync_persisted_routes({CHAT: PROFILE}, _load=load, _save=save) == {CHAT: True}
    assert state["saves"] == 1, "unchanged table is not rewritten"


class _Registry:
    def __init__(self, homes):
        self._homes = {h.resolve() for h in homes}

    def for_home(self, home):
        return object() if pathlib.Path(home).resolve() in self._homes else None


def _runner(served, multiplex=True, routes=None):
    tmp = pathlib.Path(tempfile.mkdtemp())
    homes = [("default", tmp)] + [(n, tmp / "profiles" / n) for n in served]
    for _, h in homes:
        h.mkdir(parents=True, exist_ok=True)
    config = SimpleNamespace(multiplex_profiles=multiplex, _runtime_profile_homes=tuple(homes),
                             profile_routes=list(routes or []))
    return SimpleNamespace(config=config, session_authorities=_Registry([h for _, h in homes]))


def test_live_route_requires_served_profile():
    runner = _runner(served=[])
    assert not ga.profile_served_live(runner, PROFILE)
    assert not ga.set_live_route(runner, CHAT, PROFILE)
    assert runner.config.profile_routes == []
    assert not ga.set_live_route(SimpleNamespace(config=None), CHAT, PROFILE)
    assert not ga.profile_served_live(_runner(served=[PROFILE], multiplex=False), PROFILE)


def test_live_route_add_replace_remove_in_place():
    pre = ProfileRoute(name="user", platform="status", profile="group-test", chat_id=CHAT, user_id="0x04aa")
    runner = _runner(served=[PROFILE, "group-test"], routes=[pre])
    table = runner.config.profile_routes
    assert ga.profile_served_live(runner, PROFILE)
    assert ga.set_live_route(runner, CHAT, PROFILE)
    assert runner.config.profile_routes is table, "mutated in place so existing readers see it"
    assert ga.live_route_profile(runner, CHAT) == PROFILE
    assert table[0] is pre, "most-specific-first order kept (user_id route outranks chat route)"
    assert ga.set_live_route(runner, CHAT, PROFILE) and len(table) == 2, "idempotent"
    assert ga.set_live_route(runner, CHAT, "group-test")
    assert ga.live_route_profile(runner, CHAT) == "group-test" and len(table) == 2
    assert ga.set_live_route(runner, CHAT, None)
    assert ga.live_route_profile(runner, CHAT) is None and table == [pre]


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback
                print(f"FAIL {name}: {e}")
                traceback.print_exc()
    sys.exit(1 if failed else 0)
