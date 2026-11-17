# hermes-plugin-status

Talk to your [Hermes](https://github.com/NousResearch/hermes-agent) agent over
[Status](https://status.app), the decentralized, end-to-end encrypted messenger built on Waku.

Hermes has no built-in Status support. This repo is a Hermes platform plugin plus the
setup around it (systemd units, helper scripts and a runbook). Use it to put a
Status-connected Hermes agent on a new machine quickly.

```
Status app (you) ⇄ Waku network ⇄ status-backend (127.0.0.1:21405) ⇄ this plugin ⇄ Hermes gateway ⇄ agent
```

- **status-backend** is status-go's HTTP/RPC server. It does the encryption and networking.
  Build it from [status-im/status-go](https://github.com/status-im/status-go). It is
  **not** shipped here, because Status updates it on its own schedule.
- **The plugin** logs into the backend, checks for new messages every 15 seconds and turns
  them into Hermes messages. Replies go back through the backend's send calls.

## Features

- **DMs and group chats.** DMs are answered only from allowed chat keys. Groups are joined
  only if an allowed key is a member.
- **Mention gate in groups.** The agent replies only when it is @-mentioned, quote-replied
  to, or addressed with a typed `@<ASSISTANT_NAME>`. Other group messages are saved as
  background context and don't start a turn, so they cost no model calls.
- **One agent per group.** Each group chat gets its own Hermes profile, with its own
  `SOUL.md` persona and its own memory. The profile is created automatically the first time
  the agent is mentioned in that group. Manage them with `status-agents`.
- **Proactive messages.** `send_message`, `hermes send --to status:<id>` and cron jobs
  (`deliver: status` / `deliver: origin`) all work. This includes runs from the CLI or cron,
  which happen outside the gateway process.
- **Per-group cron.** Jobs created inside a group run as that group's agent.

## Requirements

- Linux with systemd. It runs fine on a Raspberry Pi (arm64).
- Hermes installed at `~/.hermes/hermes-agent`. Tested with Hermes v0.23.1 (`5c1abd257d`).
- `status-backend` and `libsds.so` built from status-go. Tested with status-go `v10.35.5`.

## New machine: quick start

```bash
# 1. Build + install status-backend (see next section)

# 2. Install the plugin. It lands in ~/.hermes/plugins/status, which Hermes upgrades leave alone.
hermes plugins install jelly-claw/hermes-plugin-status --enable

#    The install's security scan will say "caution" because setup.sh uses sudo; that's
#    expected, so accept it. (The plugin isn't in the curated catalog: it imports Hermes
#    internals and wraps two cron functions to run group crons as the right agent, which
#    the catalog rules don't allow.)

# 3. Wire up systemd, Hermes config and the helper scripts
~/.hermes/plugins/status/setup.sh

# 4. Start the backend, then EITHER create a new bot account …
sudo systemctl start status-backend
status-create-account --name Jelly --write-env ~/.hermes/.env
#    … OR migrate an existing agent (see "Migrating an existing agent")

# 5. Add who may talk to the bot (your own chat key, from the Status app → Profile)
#    STATUS_ALLOW_FROM=0x04…   STATUS_HOME_CHANNEL=0x04…   (see .env.example)

# 6. Go
restart-hermes
journalctl -u hermes-gateway -f
```

Then, in the Status app, add the bot's chat key (printed by `status-create-account`) as a
contact and send it a message.

## Build status-backend

Follow status-go's own build docs; they are the source of truth. The outline:

```bash
# Prereqs: Go (version per status-go's go.mod), Nim (for nim-sds), make, gcc
git clone https://github.com/status-im/status-go.git
cd status-go
git checkout <latest release tag>          # e.g. v10.35.5
make status-backend                         # also fetches + builds nim-sds (libsds) next to the repo

sudo install -m 0755 build/bin/status-backend /usr/local/bin/status-backend
sudo install -D -m 0755 ../nim-sds/build/libsds.so /usr/local/lib/status-go/libsds.so
```

The unit sets `LD_LIBRARY_PATH` to `/usr/local/lib/status-go`. If you put `libsds.so`
somewhere else, run `STATUS_LIBDIR=/that/dir ./setup.sh`. To check the build:
`curl -s localhost:21405/health` should print `{"version":"v…"}`.

To upgrade status-go later: build the new tag, swap the binary and `libsds.so`, then run
`restart-hermes --full`. Your account data in `STATUS_DATA_DIR` carries over.

## Migrating an existing agent

Use this to move the agent from an old machine instead of creating a new identity. Run only
one copy at a time: two backends logged into the same account behave like two paired
devices, and both pick up the same messages.

```bash
# OLD machine
sudo systemctl stop hermes-gateway status-backend
hermes backup -o ~/hermes-backup.zip              # ~/.hermes: config, .env, profiles, sessions, cron …
tar czf ~/status-data.tgz -C ~ .status-backend    # the Status account (NOT in the Hermes backup)

# NEW machine (Hermes installed, status-backend built)
hermes import ~/hermes-backup.zip
tar xzf ~/status-data.tgz -C ~
mv ~/.hermes/plugins/status ~/.hermes/plugins/status.from-backup   # replace the copied plugin with a tracked install
hermes plugins install jelly-claw/hermes-plugin-status --enable
~/.hermes/plugins/status/setup.sh
```

Check that `STATUS_DATA_DIR` in `~/.hermes/.env` still points to the right place (your home
path may be different). Then run `restart-hermes`. Once it works, remove
`status.from-backup`, and disable the services on the old machine.

## Configuration (`~/.hermes/.env`)

| Variable | Req. | Meaning |
|---|---|---|
| `STATUS_KEY_UID` | ✓ | The bot account's key-uid (`0x…`) |
| `STATUS_PASSWORD` | ✓ | The bot account's password |
| `STATUS_DATA_DIR` | ✓ | status-backend data dir |
| `STATUS_PORT` | | Backend port (default `21405`) |
| `STATUS_PROFILE_NAME` | | Display name kept on the bot's Status profile |
| `ASSISTANT_NAME` | | Bot name; also the typed `@Name` wake word in groups (default `Jelly`) |
| `STATUS_ALLOW_FROM` | | Admin chat keys (`0x04…`). DMs only from these; groups only if one is a member |
| `STATUS_HOME_CHANNEL` | | Default target for cron / `send_message` (usually your 1:1 chat key) |
| `STATUS_ALLOW_ALL_USERS` | | `true` lets all members of an admitted group address its agent |
| `STATUS_CRON_PROFILE_SHIMS` | | `always` / `never` to override when the per-group cron shims install |

`setup.sh` also sets `multiplex_profiles: true` and `group_sessions_per_user: false` in
`~/.hermes/config.yaml`. These are needed for per-group agents and for one shared
conversation per group.

> ⚠️ **Security:** with `STATUS_ALLOW_ALL_USERS=true`, members of any group you're in can
> address the agent, and they get its full toolset (including `terminal` if
> `platform_toolsets.status` enables it). Restrict `platform_toolsets.status` or leave this
> off if your groups include people you don't fully trust.

## Group agents: `status-agents`

```
status-agents list                     group chats and the profile serving each
status-agents create <profile> <chat>  create/assign a custom-named agent
status-agents optout <chat>            serve this group with the main agent
status-agents optin <chat>             give it its own agent again
status-agents soul <chat>              path of the agent's SOUL.md (its persona)
status-agents routes                   re-sync profile_routes in config.yaml
```

`<chat>` can be a chat ID or part of the group's name. The mapping is stored in
`~/.hermes/status_agents.json`. Hermes only lets a group reach its agent if `profile_routes`
in `config.yaml` sends that chat to the agent's profile. `status-agents` keeps the two in
sync.

## Operations

- **`restart-hermes`** is the restart to use day to day. It restarts `status-backend` only
  if the backend is unhealthy, then gently restarts the gateway, letting in-progress replies
  finish. `--full` force-restarts both; `--backend-only` leaves the gateway alone.
- **Don't restart `status-backend` casually.** A restart drops the Waku connections and
  resets group-chat syncing. Reconnecting and catching up takes a while. Restarting only the
  gateway is always safe, because the plugin reuses the backend's existing login.
- **Health probes**: POST to `http://127.0.0.1:21405/statusgo/CallRPC` with
  `{"jsonrpc":"2.0","id":1,"method":"wakuext_peers","params":[]}`. A non-empty `result`
  means the transport is up. `wakuext_activeChats` works even while the transport is down,
  so it is **not** a sign that things are working.
- The gateway hides INFO logs by default. In `journalctl -u hermes-gateway`, look for
  `✗ status error` or `hermes_plugins.status` warnings.

## After every `hermes update`

`hermes doctor` can report "All checks passed" while the Status adapter is failing. So after
each update:

1. `git -C ~/.hermes/hermes-agent status --short`: any `UU` line is a merge conflict. Fix it
   with `git checkout HEAD -- <file>`.
2. Compare the plugin's overridden methods (`connect`, `send`, `disconnect`,
   `get_chat_info`, `format_message`) with `gateway/platforms/base.py`. An upstream signature
   change once took Status down without any obvious error.
3. `restart-hermes`, then `journalctl -u hermes-gateway --since -5min | grep -i status`.
   Also look for `Status cron shim` warnings.
4. Run the tests (below).

Never patch the Hermes core tree for Status behaviour. Put it in this plugin, using the
`PlatformEntry` hooks.

## Tests

No pytest is needed. Run the tests with the Hermes venv python, from the installed plugin
directory. They import the plugin as the `status` package:

```bash
cd ~/.hermes/plugins/status
PY=~/.hermes/hermes-agent/venv/bin/python
$PY -c 'import importlib.util as u,sys
for f in ["test_send_targets.py","test_profile_routes.py"]:
    s=u.spec_from_file_location(f[:-3],f); m=u.module_from_spec(s); s.loader.exec_module(m)
    for n,fn in vars(m).items():
        if n.startswith("test_"): fn(); print("PASS",n)'
$PY test_route_admission.py      # gateway admission scenarios A–D
```

Set `HERMES_AGENT_DIR` if Hermes isn't installed at `~/.hermes/hermes-agent`.

## Credits

The adapter was ported from the Status channel of the
[NanoClaw Status fork](https://github.com/yjkellyjoo/nanoclaw). NanoClaw itself is MIT,
© Gavriel. Released under the MIT license.
