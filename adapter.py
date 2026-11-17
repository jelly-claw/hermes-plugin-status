"""
Status messenger platform adapter for Hermes.

Connects to a local status-backend HTTP/RPC API and optional WebSocket signals
stream. Inbound Status messages are delivered as MessageEvents; outbound Hermes
responses are sent via Waku RPC.

Ported from NanoClaw's TypeScript StatusChannel + status-api.ts.

Required env vars:
  STATUS_KEY_UID      - Account key UID (from status-backend / Status desktop)
  STATUS_PASSWORD     - Account password
  STATUS_DATA_DIR     - status-backend data directory path

Optional env vars:
  STATUS_PORT         - status-backend HTTP port (default: 21405)
  STATUS_PROFILE_NAME - Display name to maintain on the Status profile
  STATUS_ALLOW_FROM   - Space/comma-separated admin public keys (0x04...)
                        When set: DMs only from listed keys; groups only when
                        an admin member is present. Contact requests from
                        these keys are accepted automatically.
  ASSISTANT_NAME      - Bot name prepended to outbound messages (default: Jelly)
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import httpx

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

from . import group_agents

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

POLL_INTERVAL = 15.0
HEALTH_RETRY_INTERVAL = 5.0
HEALTH_MAX_RETRIES = 30          # ~2.5 min total
CHAT_PAGE_SIZE = 50
WS_RECONNECT_DELAY = 5.0
_WS_SIGNAL_TYPES = {"messages.new", "message.delivered"}

_PUBKEY_RE = re.compile(r"^0x(?:04|00)[0-9a-fA-F]+$")
_GROUP_MEMBER_KEY_FIELDS = ("id", "publicKey", "pubKey", "pk", "key")
# Structural Status mentions appear in message text as "@0x04<130 hex chars>".
_MENTION_TOKEN_RE = re.compile(r"@(0x(?:04|00)[0-9a-fA-F]{40,})")

# send_message target syntax (PlatformEntry.target_ref_syntax_fn). Two native
# forms, both explicit targets:
#   1:1 DM     -> a contact pubkey "0x04..." (or "0x00...")
#   group chat -> the composite "<uuid>-<creator-pubkey>"
# These mirror _PUBKEY_RE above, which is what the send RPCs branch on (pubkey ->
# wakuext_sendOneToOneMessage, else wakuext_sendGroupChatMessage). Without a
# parser a raw group id matches none of core's generic id rules, so
# `hermes send --to status:<group-id>` fails with "Could not resolve" (and on
# cores before 2026-11 it silently fell back to STATUS_HOME_CHANNEL, i.e.
# delivered a group message into the operator's DM).
_SEND_PUBKEY_TARGET_RE = re.compile(r"^\s*(0x(?:04|00)[0-9a-fA-F]+)\s*$")
_SEND_GROUP_TARGET_RE = re.compile(r"^\s*([0-9a-fA-F-]{36}-0x(?:04|00)[0-9a-fA-F]+)\s*$")


def parse_send_target(ref: str):
    """``(chat_id, None)`` for a native Status id; None lets core try its generic
    rules and then the channel directory (human-readable chat names)."""
    for regex in (_SEND_GROUP_TARGET_RE, _SEND_PUBKEY_TARGET_RE):
        match = regex.fullmatch(ref or "")
        if match:
            return match.group(1), None
    return None

_rpc_seq = 0


def _is_status_pubkey(value: str) -> bool:
    return bool(_PUBKEY_RE.match(value or ""))


# ---------------------------------------------------------------------------
# status-backend HTTP / JSON-RPC helpers
# ---------------------------------------------------------------------------

def _base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


async def _post(client: httpx.AsyncClient, port: int, path: str, body: dict) -> Any:
    resp = await client.post(
        f"{_base_url(port)}{path}",
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


async def health_check(client: httpx.AsyncClient, port: int) -> bool:
    try:
        resp = await client.get(f"{_base_url(port)}/health", timeout=5.0)
        return resp.status_code < 400
    except Exception:
        return False


async def initialize_application(client: httpx.AsyncClient, port: int, data_dir: str) -> Any:
    return await _post(client, port, "/statusgo/InitializeApplication", {"dataDir": data_dir})


async def login_account(client: httpx.AsyncClient, port: int, key_uid: str, password: str) -> Any:
    return await _post(client, port, "/statusgo/LoginAccount", {"keyUID": key_uid, "password": password})


async def call_rpc(client: httpx.AsyncClient, port: int, method: str, params: list = None) -> Any:
    global _rpc_seq
    _rpc_seq += 1
    body = {"jsonrpc": "2.0", "method": method, "params": params or [], "id": _rpc_seq}
    resp = await client.post(
        f"{_base_url(port)}/statusgo/CallRPC",
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=30.0,
    )
    resp.raise_for_status()
    # status-backend sometimes double-encodes the response
    text = resp.text
    try:
        outer = json.loads(text)
        data = json.loads(outer) if isinstance(outer, str) else outer
    except Exception:
        raise RuntimeError(f"CallRPC {method}: invalid JSON")
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"RPC {method}: {data['error']}")
    return data.get("result") if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Standalone sender (out-of-process: CLI / cron, no live gateway adapter)
# ---------------------------------------------------------------------------

async def status_standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files=None,
    force_document: bool = False,
) -> dict:
    """Send a Status message without a live gateway adapter.

    Used by ``tools/send_message_tool`` when the caller (the `hermes` CLI, or a
    cron job) runs in a different process from the gateway, so there's no live
    in-process adapter to delegate to.

    status-backend is a shared local service that the gateway already logged in
    and started the messenger on — so we ONLY call the send RPC here. We must
    never call InitializeApplication/LoginAccount/startMessenger from this path:
    re-running those against a live backend wedges it (every wakuext_* RPC then
    returns -32603).
    """
    port = int(os.getenv("STATUS_PORT", "21405"))
    name = os.getenv("ASSISTANT_NAME", "Jelly")
    text = f"{name}: {message}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if _is_status_pubkey(chat_id):
                await call_rpc(
                    client, port, "wakuext_sendOneToOneMessage",
                    [{"id": chat_id, "message": text}],
                )
            else:
                try:
                    await call_rpc(
                        client, port, "wakuext_sendGroupChatMessage",
                        [{"id": chat_id, "message": text}],
                    )
                except Exception:
                    await call_rpc(
                        client, port, "wakuext_sendChatMessage",
                        [{"chatId": chat_id, "text": text}],
                    )
        return {"success": True}
    except Exception as e:
        return {"error": f"Status standalone send failed: {e}"}


# ---------------------------------------------------------------------------
# StatusAdapter
# ---------------------------------------------------------------------------

class StatusAdapter(BasePlatformAdapter):
    """Hermes gateway adapter for the Status decentralized messenger."""

    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, config: PlatformConfig):
        # Platform("status") triggers Platform._missing_(), which mints a
        # STATUS pseudo-member because this plugin is registered in the
        # platform_registry. Passing the bare string would leave
        # source.platform as a str and break gateway auth (source.platform.value).
        super().__init__(config, Platform("status"))

        extra = config.extra or {}
        self.port = int(os.getenv("STATUS_PORT", extra.get("port", "21405")))
        self.key_uid = os.getenv("STATUS_KEY_UID", extra.get("key_uid", ""))
        self.password = os.getenv("STATUS_PASSWORD", extra.get("password", ""))
        # Expand ~ / $HOME so a portable value survives moving to a machine
        # with a different home directory.
        self.data_dir = os.path.expanduser(os.path.expandvars(
            os.getenv("STATUS_DATA_DIR", extra.get("data_dir", ""))))
        self.profile_name = os.getenv("STATUS_PROFILE_NAME", extra.get("profile_name", "")).strip()
        self.assistant_name = os.getenv("ASSISTANT_NAME", "Jelly")
        # Wake-word fallback for group chats: a typed "@Jelly" (no structural
        # mention selected) should still address the bot.
        self._wake_re = re.compile(
            rf"(?<!\w)@{re.escape(self.assistant_name)}\b", re.IGNORECASE
        )

        allow_raw = os.getenv("STATUS_ALLOW_FROM", extra.get("allow_from", ""))
        self.allow_from: Set[str] = {
            pk.strip().lower()
            for pk in re.split(r"[,\s]+", allow_raw)
            if pk.strip()
        }

        self._client: Optional[httpx.AsyncClient] = None
        self._bot_pubkey: str = ""
        self._last_seen_id: Dict[str, str] = {}
        # Groups already warned about a map/route disagreement (one warning per chat).
        self._route_warned: Set[str] = set()
        self._active_chats: List[Dict] = []
        self._blocked_dm_chats: Set[str] = set()
        self._logged_blocked_groups: Set[str] = set()
        # Admin keys whose contact request failed to accept (warn once, then debug).
        self._contact_accept_failed: Set[str] = set()

        self._poll_task: Optional[asyncio.Task] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._startup_task: Optional[asyncio.Task] = None
        self._polling = False
        self._messenger_ready = False

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        # ``is_reconnect`` is passed by the reconnect watcher (core >= 2026-11); Status
        # has no server-side update queue to preserve, so a reconnect is a cold connect.
        self._client = httpx.AsyncClient(timeout=30.0)

        logger.info("Status: waiting for status-backend on port %d…", self.port)
        for attempt in range(HEALTH_MAX_RETRIES):
            if await health_check(self._client, self.port):
                break
            if attempt == HEALTH_MAX_RETRIES - 1:
                logger.error("Status: status-backend not healthy after %d retries", HEALTH_MAX_RETRIES)
                await self._client.aclose()
                self._client = None
                return False
            await asyncio.sleep(HEALTH_RETRY_INTERVAL)
        logger.info("Status: status-backend healthy")

        # status-backend persists across gateway restarts. Re-running
        # InitializeApplication / LoginAccount on a backend that's already
        # logged in wedges it (every wakuext_* RPC then returns
        # -32603 "method handler crashed"). So only init+login when the
        # backend is NOT already logged in.
        # _is_logged_in() works via settings_getSettings, which only responds
        # once the Waku messenger is running — so a True result means the
        # session is live AND the transport is up (safe to reuse, no
        # startMessenger needed). False means we must init+login and then
        # explicitly start the messenger.
        if await self._is_logged_in():
            logger.info("Status: status-backend already logged in — reusing session")
            reuse = True
        else:
            reuse = False
            try:
                init = await initialize_application(self._client, self.port, self.data_dir)
                logger.info("Status: application initialized")
                # InitializeApplication lists the accounts found in dataDir.
                # If ours isn't there, LoginAccount fails with an opaque error;
                # the usual cause is a stale path (e.g. the old machine's home
                # directory after migrating), so say that plainly.
                if isinstance(init, dict) and isinstance(init.get("accounts"), list) \
                        and not any(a.get("key-uid") == self.key_uid for a in init["accounts"]):
                    raise RuntimeError(
                        f"account {self.key_uid[:20]}… not found in STATUS_DATA_DIR={self.data_dir!r} "
                        f"({len(init['accounts'])} account(s) there). Check the path — after "
                        "migrating, it may still point at the old machine's home directory "
                        "(see README, 'Migrating an existing agent')."
                    )
                await login_account(self._client, self.port, self.key_uid, self.password)
                logger.info("Status: logged in (key_uid=%s…)", self.key_uid[:20])
            except Exception as e:
                logger.error("Status: init/login failed: %s", e)
                await self._client.aclose()
                self._client = None
                return False

        # NOTE: settings RPCs (getSettings/saveSetting) only become available
        # AFTER the Waku messenger is running, so bot-pubkey load + profile-name
        # sync happen in _ensure_messenger once the messenger is ready, not here.

        # The gateway enforces a ~30s timeout on connect(). Waku peer discovery
        # ("wakuext_startMessenger") can take longer than that, so we DON'T block
        # connect on it — return now and bring the messenger up in the
        # background. The poll loop holds until _messenger_ready is set.
        self._running = True
        self._messenger_ready = False
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._ws_task = asyncio.create_task(self._ws_loop())
        self._startup_task = asyncio.create_task(self._ensure_messenger(reuse))

        logger.info(
            "Status: connected (login OK, pubkey=%s…, allow_from=%d keys; "
            "Waku messenger starting in background)",
            self._bot_pubkey[:20] if self._bot_pubkey else "?",
            len(self.allow_from),
        )
        return True

    async def _is_logged_in(self) -> bool:
        """Return True if status-backend already has an account logged in.

        settings_getSettings returns the account (with public-key) when logged
        in and errors otherwise. Used to make connect() idempotent so we never
        double-initialize a live backend.
        """
        try:
            result = await call_rpc(self._client, self.port, "settings_getSettings", [])
            return isinstance(result, dict) and bool(result.get("public-key"))
        except Exception:
            return False

    async def _messenger_probe(self) -> bool:
        """Return True if the Waku messenger is up (wakuext RPCs respond)."""
        try:
            await call_rpc(self._client, self.port, "wakuext_activeChats", [])
            return True
        except Exception:
            return False

    async def _ensure_messenger(self, reuse: bool) -> None:
        """Bring the Waku messenger up in the background.

        Kept out of connect() so the gateway's 30s connect timeout isn't tripped.

        CRITICAL: ``wakuext_startMessenger`` is what actually starts the Waku
        TRANSPORT (it returns the store/mailserver node config and connects to
        peers). On a fresh login the transport is NOT running, and it MUST be
        started or the bot can list chats / send but never RECEIVE. We cannot
        gauge readiness from ``wakuext_activeChats`` alone — that reads the local
        DB and succeeds even with the transport down, which is exactly the bug
        that left Jelly unable to receive messages.

        - reuse=True:  session already live (settings RPC responded → transport
                       already up). Don't call startMessenger (it errors when
                       already running); just finalize.
        - reuse=False: fresh login → call startMessenger until it returns a real
                       result (transport up). -32601 right after login (method
                       not registered yet) is transient → retry.
        """
        if reuse:
            self._messenger_ready = True
            logger.info("Status: Waku messenger already running (reused session)")
            await self._post_messenger_ready()
            return

        for _ in range(HEALTH_MAX_RETRIES):
            if not self._running:
                return
            try:
                result = await call_rpc(
                    self._client, self.port, "wakuext_startMessenger", [],
                )
                if result is not None:
                    self._messenger_ready = True
                    logger.info("Status: Waku messenger started (transport up)")
                    await self._post_messenger_ready()
                    return
            except Exception as e:
                # Transport may already be running (started by a prior instance):
                # accept if wakuext RPCs respond. Otherwise it's a transient
                # post-login -32601 — keep retrying.
                if await self._messenger_probe():
                    self._messenger_ready = True
                    logger.info("Status: Waku messenger running (startMessenger: %s)", e)
                    await self._post_messenger_ready()
                    return
                logger.debug("Status: startMessenger not ready yet: %s", e)
            await asyncio.sleep(HEALTH_RETRY_INTERVAL)
        logger.warning(
            "Status: Waku messenger failed to start after %d retries",
            HEALTH_MAX_RETRIES,
        )

    async def _post_messenger_ready(self) -> None:
        """Finalize once the Waku transport is confirmed up."""
        await self._load_settings()
        await self._sync_profile_name()
        await self._ensure_allowed_chats_active()
        await self._accept_admin_contact_requests()
        await self._refresh_active_chats()

    async def _check_session(self) -> None:
        """Called when a chat refresh failed while the messenger was up.

        status-backend keeps no login across its own restart (a Docker Desktop
        quit, ``docker restart``, a crash) and sends no signal about it: every
        wakuext_* RPC just answers -32601 from then on, so the bot goes deaf
        until the gateway is restarted. connect() already waits for the backend
        and logs in (reusing a live session), so hand recovery to the gateway's
        reconnect watcher as a retryable fatal error instead of logging in again
        here. A refresh that failed while the backend is up and still logged in
        is transient and left alone.
        """
        if not self._running or not self._client:
            return
        if not await health_check(self._client, self.port):
            reason = f"status-backend not answering on port {self.port}"
        elif not await self._is_logged_in():
            reason = "status-backend lost the login session (backend restarted?)"
        else:
            return
        if getattr(self, "_fatal_error_handler", None) is None:
            # Core without the fatal-error hook: nothing would reconnect us.
            logger.warning("Status: %s — restart the gateway to log in again", reason)
            return
        logger.warning("Status: %s — handing to the gateway to reconnect", reason)
        self._set_fatal_error("status_session_lost", reason, retryable=True)
        await self._notify_fatal_error()

    async def _accept_admin_contact_requests(self) -> None:
        """Accept pending contact requests from STATUS_ALLOW_FROM keys.

        A new bot account starts with no contacts, and an admin's contact
        request (contentType 11) is never delivered as a message, so without
        this the bot silently ignores its own operator. Only admin keys are
        accepted; requests from anyone else are left for a human to decide.
        """
        if not self.allow_from or not self._client:
            return
        try:
            contacts = await call_rpc(self._client, self.port, "wakuext_contacts", [])
        except Exception as e:
            logger.debug("Status: listing contacts failed: %s", e)
            return
        for contact in contacts if isinstance(contacts, list) else []:
            pk = str(contact.get("id") or "")
            if pk.lower() not in self.allow_from:
                continue
            if not contact.get("hasAddedUs") or contact.get("added"):
                continue  # no pending request from them, or already accepted
            try:
                await call_rpc(
                    self._client, self.port,
                    "wakuext_acceptLatestContactRequestForContact", [{"id": pk}],
                )
                self._contact_accept_failed.discard(pk.lower())
                logger.info("Status: accepted contact request from admin %s…", pk[:20])
            except Exception as e:
                first = pk.lower() not in self._contact_accept_failed
                self._contact_accept_failed.add(pk.lower())
                (logger.warning if first else logger.debug)(
                    "Status: accepting contact request from %s… failed: %s", pk[:20], e)

    async def disconnect(self) -> None:
        self._running = False
        for task in (self._poll_task, self._ws_task, self._startup_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._poll_task = None
        self._ws_task = None
        self._startup_task = None
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("Status: disconnected")

    # -----------------------------------------------------------------------
    # Init helpers
    # -----------------------------------------------------------------------

    async def _load_settings(self) -> None:
        try:
            result = await call_rpc(self._client, self.port, "settings_getSettings", [])
            if isinstance(result, dict):
                pk = result.get("public-key")
                if isinstance(pk, str):
                    self._bot_pubkey = pk
                    logger.info("Status: bot pubkey=%s…", pk[:20])
        except Exception as e:
            logger.warning("Status: failed to load settings: %s", e)

    async def _sync_profile_name(self) -> None:
        if not self.profile_name:
            return
        try:
            await call_rpc(
                self._client, self.port,
                "settings_saveSetting", ["display-name", self.profile_name],
            )
            logger.info("Status: profile name set to %r", self.profile_name)
        except Exception as e:
            logger.warning("Status: failed to set profile name: %s", e)

    async def _ensure_allowed_chats_active(self) -> None:
        for pk in self.allow_from:
            try:
                await call_rpc(
                    self._client, self.port,
                    "wakuext_createOneToOneChat", [{"id": pk}],
                )
            except Exception as e:
                logger.debug("Status: createOneToOneChat(%s…) failed: %s", pk[:16], e)

    async def _refresh_active_chats(self) -> bool:
        """Refresh self._active_chats; return False when neither RPC answered."""
        try:
            result = await call_rpc(self._client, self.port, "wakuext_activeChats", [])
            self._active_chats = result if isinstance(result, list) else []
            logger.debug("Status: %d active chats", len(self._active_chats))
            return True
        except Exception:
            try:
                result = await call_rpc(self._client, self.port, "wakuext_chats", [])
                all_chats = result if isinstance(result, list) else []
                self._active_chats = [c for c in all_chats if c.get("active") is not False]
                return True
            except Exception as e:
                logger.warning("Status: failed to refresh chats: %s", e)
                return False

    # -----------------------------------------------------------------------
    # Message polling
    # -----------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(POLL_INTERVAL)
                if self._running:
                    await self._do_poll()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Status: poll loop error: %s", e)

    async def _do_poll(self) -> None:
        if self._polling or not self._client:
            return
        # Waku transport not up yet — self-heal backstop if _ensure_messenger
        # exhausted its retries. Require a real startMessenger result (transport
        # up); a bare activeChats probe is NOT sufficient (it works with the
        # transport down and would falsely look ready).
        if not self._messenger_ready:
            try:
                result = await call_rpc(self._client, self.port, "wakuext_startMessenger", [])
                if result is not None:
                    self._messenger_ready = True
                    logger.info("Status: Waku messenger started (via poll loop)")
                    await self._post_messenger_ready()
                else:
                    return
            except Exception:
                return
        self._polling = True
        try:
            if not await self._refresh_active_chats():
                await self._check_session()
                return
            await self._accept_admin_contact_requests()
            for chat in list(self._active_chats):
                await self._process_chat(chat)
        finally:
            self._polling = False

    async def _process_chat(self, chat: Dict) -> None:
        chat_id = chat.get("id", "")
        if not chat_id:
            return

        is_group = self._is_group_chat(chat)

        if self.allow_from:
            if not is_group and chat_id in self._blocked_dm_chats:
                return
            if is_group and not self._is_allowed_group(chat):
                if chat_id not in self._logged_blocked_groups:
                    logger.debug("Status: ignoring group %s (no admin member)", chat_id[:20])
                    self._logged_blocked_groups.add(chat_id)
                return
            self._logged_blocked_groups.discard(chat_id)

        try:
            result = await call_rpc(
                self._client, self.port,
                "wakuext_chatMessages",
                [chat_id, "", CHAT_PAGE_SIZE],
            )
            raw = (result or {}) if isinstance(result, dict) else {}
            messages = sorted(
                raw.get("messages", []),
                key=lambda m: m.get("timestamp", 0),
            )
        except Exception as e:
            logger.warning("Status: fetch messages for %s failed: %s", chat_id[:20], e)
            return

        if not messages:
            return

        # Prime cursor on first visit
        if chat_id not in self._last_seen_id:
            if self.allow_from and not is_group:
                if not self._is_admin_initiated(messages):
                    self._blocked_dm_chats.add(chat_id)
                    logger.debug("Status: blocking DM %s (not admin-initiated)", chat_id[:20])
            self._last_seen_id[chat_id] = messages[-1]["id"]
            return

        # Find new messages since cursor
        last_seen = self._last_seen_id[chat_id]
        last_idx = next(
            (i for i, m in enumerate(messages) if m["id"] == last_seen), -1
        )
        new_messages = messages[last_idx + 1:] if last_idx != -1 else messages

        if not new_messages:
            return

        self._last_seen_id[chat_id] = new_messages[-1]["id"]

        for msg in new_messages:
            if self._is_deliverable(msg):
                await self._dispatch(chat, chat_id, msg, is_group)

    def _is_deliverable(self, msg: Dict) -> bool:
        if msg.get("contentType") != 1:
            return False
        text = msg.get("text") or ""
        if not text:
            return False
        if self._bot_pubkey and msg.get("from") == self._bot_pubkey:
            return False
        if text.startswith(f"{self.assistant_name}: "):
            return False
        return True

    async def _dispatch(self, chat: Dict, chat_id: str, msg: Dict, is_group: bool) -> None:
        sender = msg.get("from", "")
        sender_name = msg.get("alias") or sender[:10]
        text = msg.get("text", "")
        chat_type = "group" if is_group else "dm"
        chat_name = chat.get("name") or (chat_id[:20] if is_group else sender_name)

        ts_ms = msg.get("timestamp", 0)
        try:
            timestamp = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        except Exception:
            timestamp = datetime.now(tz=timezone.utc)

        profile: Optional[str] = None
        channel_prompt: Optional[str] = None
        if is_group:
            try:
                profile = group_agents.resolve_profile_name(chat_id, chat_name, True)
            except Exception as e:
                logger.warning(
                    "Status: profile resolution failed for %s: %s", chat_id[:20], e
                )
                profile = None

            if not self._is_group_triggered(msg):
                # Unaddressed group chatter: record as observed context on the
                # group's shared session, don't wake the agent.
                self._observe_group_message(
                    chat_id, chat_name, profile, sender, sender_name, text, msg
                )
                return

            text = self._clean_group_text(text)
            if not text:
                return
            text = self._attributed_text(sender_name, sender, text)
            channel_prompt = self._group_channel_prompt(chat_name)

            if profile:
                # Lazy profile-dir creation on the first triggered turn
                # (copytree — keep it off the event loop).
                pdir = await asyncio.to_thread(
                    group_agents.ensure_profile_dir, profile, chat_name
                )
                if pdir is None:
                    profile = None  # fall back to main Jelly, still answer

        fields = dict(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=sender,
            user_name=sender_name,
        )
        source = (self._reconcile_group_profile(fields, profile) if is_group
                  else self.build_source(**fields))
        profile = getattr(source, "profile", None) or None  # where the gateway will run it

        event = MessageEvent(
            source=source,
            text=text,
            message_type=MessageType.TEXT,
            message_id=msg.get("id"),
            timestamp=timestamp,
            channel_prompt=channel_prompt,
        )

        logger.info(
            "Status: ← %s in %s (profile=%s): %s",
            sender[:16], chat_id[:16], profile or "default", text[:60],
        )
        await self.handle_message(event)

    # -----------------------------------------------------------------------
    # Group trigger gate + observed context (OpenClaw-style group UX)
    # -----------------------------------------------------------------------

    def _is_group_triggered(self, msg: Dict) -> bool:
        """Whether a group message addresses the bot.

        True on a structural @-mention (status-go computes ``mentioned`` for
        the logged-in account), a quote-reply to one of the bot's messages,
        or a typed wake word ("@Jelly" without the mention picker — which
        does NOT set ``mentioned``).
        """
        if msg.get("mentioned"):
            return True
        quoted = msg.get("quotedMessage")
        if (
            isinstance(quoted, dict)
            and self._bot_pubkey
            and quoted.get("from") == self._bot_pubkey
        ):
            return True
        return bool(self._wake_re.search(msg.get("text") or ""))

    def _clean_group_text(self, text: str) -> str:
        """Strip the bot's own mention markup / wake word from a triggered message.

        Structural mentions appear as "@0x04<pubkey>" in the raw text; remove
        those that target the bot so replies read naturally and a leading
        "/new" etc. stays coercible as a gateway command.
        """
        def _sub(m: "re.Match[str]") -> str:
            return "" if m.group(1) == self._bot_pubkey else m.group(0)

        cleaned = _MENTION_TOKEN_RE.sub(_sub, text or "")
        # One leading typed wake word ("@Jelly …") is also address markup.
        stripped = cleaned.lstrip()
        m = self._wake_re.match(stripped)
        if m:
            cleaned = stripped[m.end():]
        return re.sub(r"^[\s,:]+", "", cleaned).strip()

    def _attributed_text(self, sender_name: str, sender: str, text: str) -> str:
        """Prefix group messages with sender attribution for the shared session.

        Gateway commands ("/new", "/stop", …) must stay at the start of the
        text to be coerced, so they are passed through unattributed.
        """
        if text.lstrip().startswith("/"):
            return text.strip()
        return f"[{sender_name}|{sender[:16]}]\n{text}"

    def _group_channel_prompt(self, chat_name: str) -> str:
        bot_pk = f"{self._bot_pubkey[:20]}…" if self._bot_pubkey else "unknown"
        return (
            f"You are handling a message in the Status group chat {chat_name!r}.\n"
            f"- Your identity here: {self.assistant_name} (pubkey {bot_pk}). "
            "You only receive group messages that @-mention you or reply to you.\n"
            "- Group members' messages are attributed with a [sender|pubkey] "
            "first line.\n"
            "- Earlier group chatter may appear in the transcript as observed "
            "context that was NOT addressed to you. Treat only the current "
            "message as a request directed at you; use observed context when "
            "the current message asks about it."
        )

    def _reconcile_group_profile(self, fields: Dict[str, Any], mapped: Optional[str]):
        """Build a group's source and make the gateway route it to *mapped* (the status_agents.json
        assignment; None = main Jelly).

        The gateway admits a turn onto a non-default profile only when its own ``profile_routes``
        table routes the chat there (``profile_mismatch`` otherwise — see group_agents), and
        ``build_source`` already stamped ``source.profile`` from that table, so the route result
        is authoritative. When the map disagrees, persist the route in config.yaml and hot-add it
        to the running gateway (served profiles only), then rebuild the source so it carries the
        stamp. If that is not possible yet, the message runs where the gateway routed it — one
        warning per chat, never a crashed turn.
        """
        source = self.build_source(**fields)
        routed = getattr(source, "profile", None) or None
        if mapped == routed:
            return source
        chat_id = fields["chat_id"]
        try:
            group_agents.ensure_persisted_route(chat_id, mapped)
            if group_agents.set_live_route(self.gateway_runner, chat_id, mapped):
                rebuilt = self.build_source(**fields)
                if (getattr(rebuilt, "profile", None) or None) == mapped:
                    self._route_warned.discard(chat_id)
                    logger.info("Status: group %s now routed to profile %s",
                                chat_id[:16], mapped or "default")
                    return rebuilt
        except Exception as e:
            logger.warning("Status: could not route group %s to profile %r: %s",
                           chat_id[:16], mapped, e)
        if chat_id not in self._route_warned:
            self._route_warned.add(chat_id)
            logger.warning(
                "Status: group %s is mapped to profile %r but the gateway routes it to %r; "
                "answering as %r until the profile is served and routed (config.yaml "
                "profile_routes — `jelly-agents routes`, then restart-hermes if it persists)",
                chat_id[:16], mapped or "default", routed or "default", routed or "default",
            )
        return source

    def _observe_group_message(
        self,
        chat_id: str,
        chat_name: str,
        profile: Optional[str],
        sender: str,
        sender_name: str,
        text: str,
        msg: Dict,
    ) -> None:
        """Append unaddressed group chatter to the group session transcript
        without dispatching an agent turn (ported from the Telegram adapter's
        observe_unmentioned_group_messages pattern)."""
        store = getattr(self, "_session_store", None)
        if not store or not text:
            return
        try:
            source = self._reconcile_group_profile(dict(
                chat_id=chat_id,
                chat_name=chat_name,
                chat_type="group",
                user_id=sender,
                user_name=sender_name,
            ), profile)
            profile = getattr(source, "profile", None) or None
            session_entry = store.get_or_create_session(source)
            entry = {
                "role": "user",
                "content": f"[{sender_name}|{sender[:16]}]\n{text}",
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "observed": True,
            }
            if msg.get("id"):
                entry["message_id"] = str(msg["id"])
            store.append_to_transcript(session_entry.session_id, entry)
            logger.info(
                "Status: observed group msg in %s (no trigger, profile=%s)",
                chat_id[:16], profile or "default",
            )
        except Exception as e:
            logger.warning("Status: observe failed for %s: %s", chat_id[:16], e)

    # -----------------------------------------------------------------------
    # WebSocket (real-time poll triggers)
    # -----------------------------------------------------------------------

    async def _ws_loop(self) -> None:
        while self._running:
            try:
                await self._ws_connect()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Status: WebSocket error: %s", e)
            if self._running:
                await asyncio.sleep(WS_RECONNECT_DELAY)

    async def _ws_connect(self) -> None:
        ws_url = f"ws://127.0.0.1:{self.port}/signals"
        import aiohttp
        logger.info("Status: WebSocket connecting to %s", ws_url)
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url, heartbeat=30) as ws:
                logger.info("Status: WebSocket connected")
                async for msg in ws:
                    if not self._running:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            signal = json.loads(msg.data)
                            if signal.get("type") in _WS_SIGNAL_TYPES:
                                asyncio.create_task(self._trigger_poll())
                        except Exception:
                            pass
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

    async def _trigger_poll(self) -> None:
        try:
            await self._do_poll()
        except Exception as e:
            logger.debug("Status: signal-triggered poll error: %s", e)

    # -----------------------------------------------------------------------
    # Sending
    # -----------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        prefixed = f"{self.assistant_name}: {content}"
        try:
            await self._send_status(chat_id, prefixed)
            logger.info("Status: → %s (%d chars)", chat_id[:20], len(prefixed))
            return SendResult(success=True)
        except Exception as e:
            logger.error("Status: send to %s failed: %s", chat_id[:20], e)
            return SendResult(success=False, error=str(e), retryable=True)

    async def _send_status(self, chat_id: str, text: str) -> None:
        if _is_status_pubkey(chat_id):
            await call_rpc(
                self._client, self.port,
                "wakuext_sendOneToOneMessage",
                [{"id": chat_id, "message": text}],
            )
        else:
            try:
                await call_rpc(
                    self._client, self.port,
                    "wakuext_sendGroupChatMessage",
                    [{"id": chat_id, "message": text}],
                )
            except Exception:
                # Fallback for older backends that only expose sendChatMessage
                await call_rpc(
                    self._client, self.port,
                    "wakuext_sendChatMessage",
                    [{"chatId": chat_id, "text": text}],
                )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about a Status chat/contact."""
        is_group = not _is_status_pubkey(chat_id)
        name = chat_id
        for chat in self._active_chats:
            if chat.get("id") == chat_id:
                name = chat.get("name") or chat_id
                is_group = self._is_group_chat(chat)
                break
        return {
            "name": name,
            "type": "group" if is_group else "dm",
            "chat_id": chat_id,
        }

    def format_message(self, content: str) -> str:
        # Status renders plain text only — strip markdown markers
        import re as _re
        text = _re.sub(r"\*\*(.+?)\*\*", r"\1", content)
        text = _re.sub(r"\*(.+?)\*", r"\1", text)
        text = _re.sub(r"```[^\n]*\n?(.*?)```", r"\1", text, flags=_re.DOTALL)
        text = _re.sub(r"`(.+?)`", r"\1", text)
        return text.strip()

    # -----------------------------------------------------------------------
    # Admin filter helpers
    # -----------------------------------------------------------------------

    def _is_group_chat(self, chat: Dict) -> bool:
        return chat.get("chatType", 1) != 1

    def _is_allowed_group(self, chat: Dict) -> bool:
        if not self.allow_from:
            return True
        members = chat.get("members", [])
        return isinstance(members, list) and any(
            (self._extract_member_pk(m) or "").lower() in self.allow_from
            for m in members
        )

    def _extract_member_pk(self, member: Any) -> Optional[str]:
        if isinstance(member, str):
            return member
        if not isinstance(member, dict):
            return None
        for field in _GROUP_MEMBER_KEY_FIELDS:
            val = member.get(field)
            if isinstance(val, str) and val:
                return val
        return None

    def _is_admin_initiated(self, messages: List[Dict]) -> bool:
        if not self.allow_from:
            return True
        for msg in messages:
            sender = msg.get("from", "")
            if self._bot_pubkey and sender == self._bot_pubkey:
                continue
            return sender.lower() in self.allow_from
        return False


# ---------------------------------------------------------------------------
# Plugin entry point (v0.17.0 plugin API: register(ctx) -> ctx.register_platform)
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register the Status gateway platform adapter.

    Called by the Hermes plugin loader for plugins under ``~/.hermes/plugins/``.
    ``ctx.register_platform`` builds a ``PlatformEntry`` and sets ``source`` /
    ``plugin_name`` itself, so we don't pass those.
    """
    def _check() -> bool:
        return bool(
            os.getenv("STATUS_KEY_UID")
            and os.getenv("STATUS_PASSWORD")
            and os.getenv("STATUS_DATA_DIR")
        )

    def _env_seed():
        # Read env vars, return PlatformConfig.extra seed when auto-enabled.
        if not _check():
            return None
        seed = {
            "port": os.getenv("STATUS_PORT", "21405"),
            "key_uid": os.getenv("STATUS_KEY_UID", ""),
            "profile_name": os.getenv("STATUS_PROFILE_NAME", ""),
            "allow_from": os.getenv("STATUS_ALLOW_FROM", ""),
            # Group chats are one shared conversation per group (the group
            # agent is a single participant in the room), not one thread per
            # member. Mirrors the root config group_sessions_per_user: false.
            "group_sessions_per_user": False,
        }
        # Home channel for proactive sends (send_message tool / cron deliver).
        # chat_id is a 1:1 pubkey (0x04…) or a composite group id.
        home = os.getenv("STATUS_HOME_CHANNEL", "").strip()
        if home:
            seed["home_channel"] = {"chat_id": home, "name": "Status Home"}
        return seed

    ctx.register_platform(
        name="status",
        label="Status",
        adapter_factory=lambda cfg: StatusAdapter(cfg),
        check_fn=_check,
        validate_config=lambda cfg: _check(),
        required_env=["STATUS_KEY_UID", "STATUS_PASSWORD", "STATUS_DATA_DIR"],
        install_hint="Set STATUS_KEY_UID, STATUS_PASSWORD, STATUS_DATA_DIR in ~/.hermes/.env",
        env_enablement_fn=_env_seed,
        emoji="\U0001f535",
        pii_safe=True,
        platform_hint=(
            "You are communicating via Status messenger — "
            "a decentralized, E2E encrypted platform built on Ethereum/Waku. "
            "Keep responses concise; markdown renders as plain text."
        ),
        allowed_users_env="STATUS_ALLOW_FROM",
        # Gateway-side sender authorization opt-out. The adapter itself is the
        # perimeter: DM chats are blocked unless initiated by an allow_from
        # admin, and groups are only processed when an admin is a member (plus
        # the mention gate). With STATUS_ALLOW_ALL_USERS=true, members of
        # those admitted groups may address the group's agent — required for
        # per-group agents to serve the whole group, since allowed_users_env
        # alone would deny every non-admin sender at the gateway.
        allow_all_env="STATUS_ALLOW_ALL_USERS",
        # Proactive delivery: home channel for the send_message tool + cron
        # `deliver=status`, and a standalone sender so the CLI/cron (which run
        # outside the gateway process) can send without a live adapter.
        cron_deliver_env_var="STATUS_HOME_CHANNEL",
        standalone_sender_fn=status_standalone_send,
        # Native target ids for send_message / `hermes send --to status:<id>`.
        target_ref_syntax_fn=parse_send_target,
    )

    # Per-group cron: stamp jobs created during profile-scoped turns and run
    # them under that profile. Gateway process only — the ticker lives there,
    # and CLI invocations shouldn't pay the import.
    try:
        is_gateway = "gateway" in sys.argv[1:4]
        override = os.getenv("STATUS_CRON_PROFILE_SHIMS", "").strip().lower()
        if override in {"1", "true", "always"} or (is_gateway and override not in {"0", "false", "never"}):
            from . import cron_profiles
            cron_profiles.install()
    except Exception as e:
        logger.warning("Status: cron profile shims not installed: %s", e)

    logger.info("Status: platform adapter registered")
