"""Lost-session recovery and admin contact-request acceptance for the Status adapter.

Fakes status-backend at the call_rpc / health_check boundary; no backend, gateway or
~/.hermes state is touched. Run with the Hermes python (see README "Tests").
"""
import asyncio
import os
import pathlib
import sys

sys.path.insert(0, os.path.expanduser(os.environ.get("HERMES_AGENT_DIR", "~/.hermes/hermes-agent")))
import hermes_bootstrap  # noqa: E402,F401  activates Hermes' isolated runtime (core deps)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import status.adapter as ad  # noqa: E402

ADMIN = "0x04" + "a" * 128
STRANGER = "0x04" + "b" * 128


class FakeBackend:
    """Stands in for status-backend: answers call_rpc / health_check from flags."""

    def __init__(self, *, healthy=True, logged_in=True, chats_ok=True, contacts=(), accept_error=None):
        self.healthy, self.logged_in, self.chats_ok = healthy, logged_in, chats_ok
        self.contacts, self.accept_error = list(contacts), accept_error
        self.calls = []

    async def health_check(self, client, port):
        return self.healthy

    async def call_rpc(self, client, port, method, params=None):
        self.calls.append((method, params))
        if method in ("wakuext_activeChats", "wakuext_chats"):
            if not self.chats_ok:
                raise RuntimeError(f"RPC {method}: {{'code': -32601}}")
            return []
        if method == "settings_getSettings":
            if not self.logged_in:
                raise RuntimeError("RPC settings_getSettings: not logged in")
            return {"public-key": "0x04" + "c" * 128}
        if method == "wakuext_contacts":
            return self.contacts
        if method == "wakuext_acceptLatestContactRequestForContact":
            if self.accept_error:
                raise RuntimeError(self.accept_error)
            return {}
        raise AssertionError(f"unexpected RPC {method}")

    def accepted(self):
        return [p[0]["id"] for m, p in self.calls if m == "wakuext_acceptLatestContactRequestForContact"]


def make_adapter(backend, *, allow_from=(ADMIN,), with_handler=True):
    ad.call_rpc, ad.health_check = backend.call_rpc, backend.health_check
    a = object.__new__(ad.StatusAdapter)
    a._client, a.port, a._running, a._polling = object(), 21405, True, False
    a._messenger_ready = True
    a.allow_from = {k.lower() for k in allow_from}
    a._active_chats, a._last_seen_id = [], {}
    a._blocked_dm_chats, a._logged_blocked_groups = set(), set()
    a._contact_accept_failed = set()
    a._fatal_error_code = a._fatal_error_message = None
    a._fatal_error_retryable = True
    a._write_runtime_status_safe = lambda *args, **kw: None  # keep tests off ~/.hermes
    a.notified = []

    async def handler(adapter):
        a.notified.append(adapter.fatal_error_code)

    a._fatal_error_handler = handler if with_handler else None
    return a


def poll(a):
    asyncio.run(a._do_poll())


def test_lost_login_session_is_handed_to_the_reconnect_watcher():
    # Regression: after status-backend restarted (Docker Desktop quit) the adapter kept
    # polling a logged-out backend forever and never received another message.
    a = make_adapter(FakeBackend(chats_ok=False, logged_in=False))
    poll(a)
    assert a.notified == ["status_session_lost"]
    assert a.fatal_error_retryable and not a._running
    assert "lost the login session" in a.fatal_error_message


def test_unreachable_backend_is_handed_to_the_reconnect_watcher():
    a = make_adapter(FakeBackend(chats_ok=False, healthy=False))
    poll(a)
    assert a.notified == ["status_session_lost"]
    assert "not answering" in a.fatal_error_message


def test_transient_refresh_failure_with_live_session_does_not_reconnect():
    a = make_adapter(FakeBackend(chats_ok=False))
    poll(a)
    assert a.notified == [] and a._running and not a.has_fatal_error


def test_core_without_fatal_handler_keeps_running():
    a = make_adapter(FakeBackend(chats_ok=False, logged_in=False), with_handler=False)
    poll(a)
    assert a._running and not a.has_fatal_error


def test_pending_admin_contact_request_is_accepted():
    # Regression: a new bot account never accepted its operator's contact request, so the
    # admin's messages never reached Hermes.
    b = FakeBackend(contacts=[{"id": ADMIN, "hasAddedUs": True, "added": False}])
    poll(make_adapter(b))
    assert b.accepted() == [ADMIN]


def test_admin_key_matches_case_insensitively():
    b = FakeBackend(contacts=[{"id": ADMIN.upper().replace("0X", "0x"), "hasAddedUs": True, "added": False}])
    poll(make_adapter(b))
    assert len(b.accepted()) == 1


def test_non_admin_and_settled_contacts_are_left_alone():
    b = FakeBackend(contacts=[
        {"id": STRANGER, "hasAddedUs": True, "added": False},  # not an admin
        {"id": ADMIN, "hasAddedUs": True, "added": True},       # already mutual
    ])
    poll(make_adapter(b))
    assert b.accepted() == []


def test_no_allow_from_accepts_nobody():
    b = FakeBackend(contacts=[{"id": STRANGER, "hasAddedUs": True, "added": False}])
    poll(make_adapter(b, allow_from=()))
    assert b.accepted() == [] and not any(m == "wakuext_contacts" for m, _ in b.calls)


def test_failed_accept_is_remembered_and_retried():
    b = FakeBackend(contacts=[{"id": ADMIN, "hasAddedUs": True, "added": False}], accept_error="boom")
    a = make_adapter(b)
    poll(a)
    poll(a)
    assert len(b.accepted()) == 2 and a._contact_accept_failed == {ADMIN.lower()}
