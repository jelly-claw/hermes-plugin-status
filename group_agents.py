"""Per-group-chat agent profiles for the Status platform plugin.

Each Status group chat gets its own Hermes profile (an independent
HERMES_HOME under ~/.hermes/profiles/<slug>/ with its own SOUL.md persona
and memories/), auto-created on the group's first triggered message.

Mapping file: ~/.hermes/status_agents.json
    {
      "auto_create": true,
      "chats": {
        "<chat_id>":  {"profile": "jelly-the-trading-bot", "name": "Jelly the trading bot"},
        "<chat_id2>": {"profile": "default", "name": "family chat"}   // opted out -> main Jelly
      }
    }

The file is mtime-cached; edits apply without a gateway restart.
Used by adapter.py (in-gateway) and the jelly-agents helper CLI.
"""

import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

MAP_FILENAME = "status_agents.json"

# Names that must never be used as auto-generated profile ids (mirrors
# hermes_cli.profiles._RESERVED_NAMES plus the built-in "default").
_RESERVED_NAMES = {"default", "hermes", "test", "tmp", "root", "sudo"}
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_lock = threading.Lock()
_cache: Dict[str, Any] = {"path": None, "mtime": None, "data": None}

_BOOTSTRAP_MARKER = "<!-- group-agent-bootstrap -->"


def _default_home() -> Path:
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root())
    except Exception:
        return Path.home() / ".hermes"


def _map_path() -> Path:
    return _default_home() / MAP_FILENAME


def _empty_map() -> Dict[str, Any]:
    return {"auto_create": True, "chats": {}}


def load_map() -> Dict[str, Any]:
    """Load the chat->profile mapping (mtime-cached). Never raises."""
    path = _map_path()
    with _lock:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return _empty_map()
        if _cache["path"] == str(path) and _cache["mtime"] == mtime:
            return _cache["data"]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("mapping root must be an object")
        except Exception as e:
            logger.warning("status_agents.json unreadable (%s) — treating as empty", e)
            return _empty_map()
        data.setdefault("auto_create", True)
        chats = data.get("chats")
        if not isinstance(chats, dict):
            data["chats"] = {}
        _cache.update(path=str(path), mtime=mtime, data=data)
        return data


def save_map(data: Dict[str, Any]) -> None:
    """Atomically write the mapping file and refresh the cache."""
    path = _map_path()
    with _lock:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
        try:
            _cache.update(path=str(path), mtime=path.stat().st_mtime, data=data)
        except OSError:
            _cache.update(path=None, mtime=None, data=None)


def record_mapping(chat_id: str, profile: str, chat_name: str) -> None:
    data = load_map()
    chats = dict(data.get("chats") or {})
    chats[chat_id] = {"profile": profile, "name": chat_name}
    updated = dict(data)
    updated["chats"] = chats
    save_map(updated)


def _hash_suffix(chat_id: str) -> str:
    return hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:6]


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:48].strip("-")
    return slug


def _profile_dir(name: str) -> Optional[Path]:
    try:
        from hermes_cli.profiles import get_profile_dir
        return Path(get_profile_dir(name))
    except Exception:
        return _default_home() / "profiles" / name


def _valid_profile_id(name: str) -> bool:
    if name in _RESERVED_NAMES:
        return False
    try:
        from hermes_cli.profiles import validate_profile_name
        validate_profile_name(name)
        return True
    except Exception:
        return bool(_PROFILE_ID_RE.match(name))


def _auto_profile_name(chat_id: str, chat_name: str, data: Dict[str, Any]) -> str:
    """Deterministic profile id for a group: slug of the name, hash-suffixed on
    collision with another chat's profile, an existing unrelated profile dir,
    or a reserved/invalid slug."""
    slug = slugify(chat_name)
    taken = {
        (entry.get("profile") or "")
        for cid, entry in (data.get("chats") or {}).items()
        if cid != chat_id and isinstance(entry, dict)
    }

    def _usable(candidate: str) -> bool:
        if not candidate or not _valid_profile_id(candidate):
            return False
        if candidate in taken:
            return False
        # A dir that exists but is not mapped to this chat belongs to someone
        # else (manually created profile) — don't hijack it.
        mapped_here = (data.get("chats") or {}).get(chat_id, {})
        if isinstance(mapped_here, dict) and mapped_here.get("profile") == candidate:
            return True
        pdir = _profile_dir(candidate)
        return not (pdir and pdir.exists())

    if _usable(slug):
        return slug
    suffixed = f"{slug[:40]}-{_hash_suffix(chat_id)}".strip("-") if slug else f"group-{_hash_suffix(chat_id)}"
    if _usable(suffixed) or suffixed not in taken:
        return suffixed
    return f"group-{_hash_suffix(chat_id)}"


def resolve_profile_name(
    chat_id: str,
    chat_name: str,
    is_group: bool,
    record: bool = True,
) -> Optional[str]:
    """Return the profile name serving this chat, or None for main Jelly.

    Explicit mapping entries win ("default"/"" -> None). Unmapped groups get
    an auto-generated profile name when auto_create is on; with record=True
    the assignment is persisted to status_agents.json. Name resolution alone
    is enough for session keys — the profile DIRECTORY is created lazily via
    ensure_profile_dir() on the first triggered turn.
    """
    if not chat_id:
        return None
    data = load_map()
    entry = (data.get("chats") or {}).get(chat_id)
    if isinstance(entry, dict) and "profile" in entry:
        prof = str(entry.get("profile") or "").strip()
        return None if prof in ("", "default") else prof
    if not is_group or not data.get("auto_create", True):
        return None
    name = _auto_profile_name(chat_id, chat_name or "", data)
    if record:
        try:
            record_mapping(chat_id, name, chat_name or "")
        except Exception as e:
            logger.warning("could not record status agent mapping for %s: %s", chat_id[:20], e)
    return name


# ---------------------------------------------------------------------------
# Profile directory creation
# ---------------------------------------------------------------------------

def _bootstrap_note(profile: str, chat_name: str, pdir: Path) -> str:
    return f"""

---

{_BOOTSTRAP_MARKER}
## Dedicated group agent

You are the dedicated agent for the Status group chat "{chat_name}".

- This SOUL.md ({pdir}/SOUL.md) is YOUR identity file. It started as a copy of
  Jelly's persona, but you are free to become your own character for this group.
- Your memory (memories/MEMORY.md and memories/USER.md next to this file) is
  scoped to this group only — remember things about this group and its members.
- When group members ask you to change your persona, personality, or role,
  edit this SOUL.md yourself with your file tools (keep this bootstrap
  section). Changes apply from the next session — tell them to send /new
  (while mentioning you) to start one.
- You only receive group messages that @-mention you or reply to your
  messages. Other group chatter may appear in your transcript as observed
  context lines prefixed [sender|pubkey]; it was not addressed to you.
"""


def _disable_status_platform(config_path: Path) -> None:
    """Ensure the profile's config.yaml carries platforms.status.enabled: false.

    Without this, the multiplexing gateway would auto-enable the status
    platform for the profile from env vars and spawn a duplicate poller on
    the shared status-backend.
    """
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    if re.search(r"(?m)^platforms\s*:", text):
        import yaml
        data = yaml.safe_load(text) or {}
        platforms = data.get("platforms") or {}
        status_cfg = platforms.get("status") or {}
        status_cfg["enabled"] = False
        platforms["status"] = status_cfg
        data["platforms"] = platforms
        config_path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    else:
        block = "\n# Added by status plugin group_agents: this profile must NOT\n# start its own status adapter (the default profile owns the poller).\nplatforms:\n  status:\n    enabled: false\n"
        config_path.write_text(text + block, encoding="utf-8")


def ensure_profile_dir(profile: str, chat_name: str) -> Optional[Path]:
    """Create the profile directory for a group agent if missing. Idempotent.

    Clones the default profile's config/SOUL/skills, strips the cloned memory
    files (group memory starts empty), disables the status platform in the
    profile's config, and appends the group-agent bootstrap note to SOUL.md.

    Returns the profile dir, or None on failure (caller falls back to main
    Jelly so the message is still answered).
    """
    try:
        from hermes_cli.profiles import create_profile, get_profile_dir
        pdir = Path(get_profile_dir(profile))
        if pdir.is_dir():
            return pdir
        create_profile(profile, clone_config=True, no_alias=True)
        if not pdir.is_dir():
            raise RuntimeError(f"create_profile did not produce {pdir}")

        # Fresh memory for the group — the clone copies Jelly's memory files.
        for rel in ("memories/MEMORY.md", "memories/USER.md"):
            try:
                (pdir / rel).unlink(missing_ok=True)
            except OSError:
                pass

        _disable_status_platform(pdir / "config.yaml")

        soul = pdir / "SOUL.md"
        existing = soul.read_text(encoding="utf-8") if soul.exists() else ""
        if _BOOTSTRAP_MARKER not in existing:
            soul.write_text(existing + _bootstrap_note(profile, chat_name, pdir), encoding="utf-8")

        logger.info("Status: created group agent profile %r at %s (group %r)", profile, pdir, chat_name)
        return pdir
    except Exception as e:
        logger.warning("Status: failed to create group agent profile %r: %s", profile, e, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Gateway profile_routes (admission)
#
# Since the 2026-11 Hermes update the gateway admits a message onto a
# non-default profile ONLY when its own ``profile_routes`` table routes that
# chat there: gateway/session_ingress_context.py::_binding re-resolves the
# route at admission and raises RuntimeStoreError('profile_mismatch') when the
# stamped ``source.profile`` disagrees — the turn errors out and the group gets
# an error reply. ``BasePlatformAdapter.build_source`` stamps ``source.profile``
# from that table, so the mapping file alone no longer selects the agent: every
# chat -> profile assignment needs a matching route in the DEFAULT profile's
# config.yaml (read at boot) and, to apply without a restart, in the running
# gateway's route table. The helpers below keep both in step with the map.
# ---------------------------------------------------------------------------

ROUTE_PLATFORM = "status"


def route_name(profile: str) -> str:
    return f"status-group:{profile}"


def _is_chat_route(route: Any, chat_id: str) -> bool:
    """A plain chat route for *chat_id* on the status platform — a dict from config.yaml or a
    parsed ``ProfileRoute`` — with no thread/user discriminator, on the default bot."""
    if isinstance(route, dict):
        get = route.get
    else:
        def get(key, default=None):
            return getattr(route, key, default)
    return (
        str(get("platform") or "") == ROUTE_PLATFORM
        and str(get("chat_id") or "") == chat_id
        and not get("thread_id")
        and not get("user_id")
        and (get("bot_profile") in (None, "", "default"))
        and get("enabled", True) is not False
    )


def persisted_route_profile(chat_id: str, routes: Optional[list] = None) -> Optional[str]:
    """Profile that config.yaml's ``profile_routes`` pin *chat_id* to (None = no plain chat route)."""
    if routes is None:
        from hermes_cli.config import load_config_readonly
        routes = load_config_readonly().get("profile_routes") or []
    for route in routes:
        if isinstance(route, dict) and _is_chat_route(route, chat_id):
            return str(route.get("profile") or "") or None
    return None


def _config_io(_load=None, _save=None):
    """Same read/write pair as `hermes config set`: the RAW user file (no schema defaults merged
    in, so nothing but this key changes on disk) and the comment-preserving save."""
    if _load is None or _save is None:
        from hermes_cli.config import get_config_path, require_readable_config_before_write, save_config
        _load = _load or (lambda: require_readable_config_before_write(get_config_path()))
        _save = _save or save_config
    return _load, _save


def _apply_route(cfg: Dict[str, Any], chat_id: str, profile: Optional[str]) -> bool:
    """Set *chat_id*'s plain chat route in *cfg* to *profile* (None = none). True when changed."""
    routes = [r for r in (cfg.get("profile_routes") or []) if isinstance(r, dict)]
    current = persisted_route_profile(chat_id, routes)
    if current == profile:
        return False
    keep = [r for r in routes if not _is_chat_route(r, chat_id)]
    if profile is not None:
        keep.append({"name": route_name(profile), "platform": ROUTE_PLATFORM,
                     "chat_id": chat_id, "profile": profile})
    cfg["profile_routes"] = keep
    logger.info("Status: config.yaml profile_routes: %s -> %s (was %s)",
                chat_id[:16], profile or "default", current or "default")
    return True


def _routable(chat_id: str, profile: Optional[str]) -> bool:
    """A route to a profile whose directory does not exist makes the gateway DROP every message
    of that chat at the next boot (rejected route, fail-closed), so it is refused."""
    if profile is None:
        return True
    pdir = _profile_dir(profile)
    if pdir is None or not pdir.is_dir():
        logger.debug("Status: not routing %s to profile %r yet: profile directory missing",
                     chat_id[:16], profile)
        return False
    return True


def ensure_persisted_route(chat_id: str, profile: Optional[str], *, _load=None, _save=None) -> bool:
    """Make config.yaml's ``profile_routes`` pin *chat_id* to *profile* (None = no route, i.e.
    main Jelly). Idempotent; returns True when the table is in the wanted state afterwards
    (False when the profile directory does not exist yet)."""
    if not _routable(chat_id, profile):
        return False
    _load, _save = _config_io(_load, _save)
    cfg = _load()
    if _apply_route(cfg, chat_id, profile):
        _save(cfg)
    return True


def sync_persisted_routes(assignments: Dict[str, Optional[str]], *, _load=None, _save=None) -> Dict[str, bool]:
    """``ensure_persisted_route`` for many chats with ONE config.yaml write. Returns
    ``{chat_id: in wanted state}``."""
    _load, _save = _config_io(_load, _save)
    cfg = _load()
    result, changed = {}, False
    for chat_id, profile in assignments.items():
        result[chat_id] = _routable(chat_id, profile)
        if result[chat_id]:
            changed = _apply_route(cfg, chat_id, profile) or changed
    if changed:
        _save(cfg)
    return result


def live_route_profile(runner: Any, chat_id: str) -> Optional[str]:
    """Profile the RUNNING gateway's route table pins *chat_id* to (None = none)."""
    routes = getattr(getattr(runner, "config", None), "profile_routes", None) or []
    for route in routes:
        if _is_chat_route(route, chat_id):
            return getattr(route, "profile", None) or None
    return None


def profile_served_live(runner: Any, profile: str) -> bool:
    """Whether the running multiplexer reserved *profile* AND built its session authority — the
    two facts admission (``_binding``) checks. A route to an unserved profile is rejected at
    ingress and the message is dropped, so callers must not add one before this holds."""
    config = getattr(runner, "config", None)
    if not getattr(config, "multiplex_profiles", False):
        return False
    reserved = getattr(config, "_runtime_profile_homes", None) or ()
    home = next((Path(h) for n, h in reserved if n == profile), None)
    if home is None:
        return False
    try:
        from gateway.session_authorities import authority_for_home
        return authority_for_home(runner, home.resolve()) is not None
    except Exception:
        return False


def set_live_route(runner: Any, chat_id: str, profile: Optional[str]) -> bool:
    """Point the running gateway's route table at *profile* for *chat_id* (None = remove the
    route) without a restart. Returns True when the table is in the wanted state afterwards,
    False when there is no runner config or the profile is not served yet (retry later)."""
    config = getattr(runner, "config", None)
    if config is None:
        return False
    if live_route_profile(runner, chat_id) == profile:
        return True
    if profile is not None and not profile_served_live(runner, profile):
        return False
    from gateway.profile_routing import ProfileRoute
    existing = getattr(config, "profile_routes", None)
    routes = [r for r in (existing or []) if not _is_chat_route(r, chat_id)]
    if profile is not None:
        routes.append(ProfileRoute(name=route_name(profile), platform=ROUTE_PLATFORM,
                                   profile=profile, chat_id=chat_id))
    routes.sort(key=lambda r: r.specificity, reverse=True)
    if isinstance(existing, list):
        existing[:] = routes  # in place: every reader holds this very list
    else:
        config.profile_routes = routes
    return True
