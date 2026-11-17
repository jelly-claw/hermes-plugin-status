#!/usr/bin/env bash
# setup.sh — wire the Hermes Status plugin into a machine (idempotent).
#
# Run as your normal user (uses sudo only for the gateway's systemd unit), AFTER:
#   1. Hermes is installed (~/.hermes/hermes-agent)
#   2. the status-backend container is running (README, "Run status-backend in Docker")
#   3. the plugin is installed: hermes plugins install jelly-claw/hermes-plugin-status --enable
#
# What it does:
#   - installs a drop-in so hermes-gateway.service starts after Docker
#   - installs the gateway service (hermes gateway install --system) if missing
#   - sets Hermes config: multiplex_profiles=true, group_sessions_per_user=false
#   - installs helpers into ~/.local/bin: restart-hermes, status-agents, status-create-account
#   - checks .env: required keys, STATUS_DATA_DIR really holds the account,
#     and no paths still point at another machine's home directory
#
# Options:
#   --force              replace existing units/helpers that differ
#   --fix-home OLD_HOME  after migrating: rewrite OLD_HOME → $HOME in the imported
#                        .env / config.yaml / cron jobs / profiles (each file backed
#                        up to <file>.bak-fixhome-<timestamp> first)
#
# Env overrides: STATUS_PORT (21405), HERMES_HOME (~/.hermes)
set -euo pipefail

FORCE=0
FIX_HOME=""
while (($#)); do
  case "$1" in
    --force)    FORCE=1 ;;
    --fix-home) FIX_HOME="${2:?--fix-home needs the old home path, e.g. /home/olduser}"; shift ;;
    -h|--help)  sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_PY="$HERMES_HOME/hermes-agent/venv/bin/python"
PORT="${STATUS_PORT:-21405}"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR=/etc/systemd/system

say()  { printf '\033[36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

hermes() { "$HERMES_PY" -m hermes_cli.main "$@"; }

# ── prerequisites ───────────────────────────────────────────────────────────
[[ -x "$HERMES_PY" ]] || die "Hermes not found at $HERMES_HOME/hermes-agent — install Hermes first"
curl -sf "http://127.0.0.1:$PORT/health" >/dev/null \
  || warn "status-backend not answering on 127.0.0.1:$PORT — start the container (README, 'Run status-backend in Docker')"
[[ -d "$HERMES_HOME/plugins/status" ]] || warn "plugin not at $HERMES_HOME/plugins/status — run: hermes plugins install jelly-claw/hermes-plugin-status --enable"

install_file() {  # install_file <src> <dest> [sudo]
  local src="$1" dest="$2" sudo="${3:-}"
  if [[ -e "$dest" && "$FORCE" -eq 0 ]]; then
    if ${sudo:+sudo} cmp -s "$src" "$dest"; then ok "$dest up to date"; else warn "$dest exists and differs — kept (use --force to replace)"; fi
    return
  fi
  ${sudo:+sudo} install -D -m "${MODE:-0644}" "$src" "$dest"
  ok "installed $dest"
}

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# Files a `hermes import` carries over that may hold absolute home paths.
path_files() {
  ls -d "$HERMES_HOME/.env" "$HERMES_HOME/config.yaml" "$HERMES_HOME/cron/jobs.json" \
        "$HERMES_HOME"/profiles/*/.env "$HERMES_HOME"/profiles/*/config.yaml 2>/dev/null || true
}

# ── migration: rewrite the old home path ────────────────────────────────────
if [[ -n "$FIX_HOME" ]]; then
  FIX_HOME="${FIX_HOME%/}"
  [[ "$FIX_HOME" == "$HOME" ]] && die "--fix-home is the current \$HOME ($HOME); pass the OLD machine's home"
  say "rewriting $FIX_HOME → $HOME"
  ts="$(date +%Y%m%d-%H%M%S)"; changed=0
  while IFS= read -r f; do
    [[ -n "$f" ]] && grep -qF "$FIX_HOME" "$f" || continue
    cp -p "$f" "$f.bak-fixhome-$ts"
    OLD_HOME="$FIX_HOME" perl -pi -e 's|\Q$ENV{OLD_HOME}\E|$ENV{HOME}|g' "$f"
    ok "fixed ${f/#$HOME/~}  (backup: .bak-fixhome-$ts)"; changed=$((changed+1))
  done < <(path_files)
  ((changed)) || ok "no file contained $FIX_HOME"
fi

# ── systemd: gateway drop-in ────────────────────────────────────────────────
say "systemd units"
install_file "$HERE/systemd/hermes-gateway.service.d/10-status-backend.conf" \
  "$UNIT_DIR/hermes-gateway.service.d/10-status-backend.conf" sudo

# ── systemd: Hermes gateway ─────────────────────────────────────────────────
if [[ -f "$UNIT_DIR/hermes-gateway.service" ]]; then
  ok "hermes-gateway.service already installed"
else
  say "installing hermes-gateway.service (system-level, runs as $USER)"
  sudo "$HERMES_PY" -m hermes_cli.main gateway install --system --run-as-user "$USER"
fi
sudo systemctl daemon-reload
sudo systemctl enable hermes-gateway.service >/dev/null 2>&1 || true
ok "units enabled"

# ── Hermes config ───────────────────────────────────────────────────────────
say "Hermes config"
[[ "$(hermes config get multiplex_profiles 2>/dev/null)" == "true" ]] \
  || hermes config set --force multiplex_profiles true
[[ "$(hermes config get group_sessions_per_user 2>/dev/null)" == "false" ]] \
  || hermes config set group_sessions_per_user false
hermes config get plugins.enabled 2>/dev/null | grep -qx '\s*- status' \
  || hermes plugins enable status || warn "could not enable plugin 'status' — is it installed?"
ok "multiplex_profiles=true, group_sessions_per_user=false, plugin enabled"

# ── helpers ─────────────────────────────────────────────────────────────────
say "helpers → $BIN_DIR"
MODE=0755 install_file "$HERE/bin/restart-hermes" "$BIN_DIR/restart-hermes"
MODE=0755 install_file "$HERE/bin/status-create-account" "$BIN_DIR/status-create-account"
# status-agents imports Hermes modules, so it runs on the Hermes venv python.
sed "1s|.*|#!$HERMES_PY|" "$HERE/bin/status-agents" > "$TMP/status-agents"
MODE=0755 install_file "$TMP/status-agents" "$BIN_DIR/status-agents"

# ── .env check ──────────────────────────────────────────────────────────────
say "checking $HERMES_HOME/.env"
missing=()
for k in STATUS_KEY_UID STATUS_PASSWORD STATUS_DATA_DIR STATUS_ALLOW_FROM STATUS_HOME_CHANNEL; do
  grep -qE "^\s*$k=." "$HERMES_HOME/.env" 2>/dev/null || missing+=("$k")
done

env_val() {  # last value of KEY in .env, quotes stripped, ~ / $HOME expanded
  local v; v="$(grep -E "^\s*$1=" "$HERMES_HOME/.env" 2>/dev/null | tail -1 | cut -d= -f2- | sed -E "s/^[\"']//; s/[\"']\s*$//")"
  v="${v/#\~/$HOME}"; v="${v//\$HOME/$HOME}"; v="${v//\$\{HOME\}/$HOME}"; printf '%s' "$v"
}

# Paths left over from another machine (a different /home/<user> or /Users/<user>).
stale="$(path_files | xargs -r grep -ohE '(/home|/Users)/[^/[:space:]"'"'"':]+' 2>/dev/null \
          | sort -u | grep -vxF "$HOME" || true)"
if [[ -n "$stale" ]]; then
  for old in $stale; do
    warn "files under $HERMES_HOME still reference $old (this machine's home is $HOME)"
    echo "    fix with: $0 --fix-home $old"
  done
fi

data_dir="$(env_val STATUS_DATA_DIR)"; key_uid="$(env_val STATUS_KEY_UID)"
if [[ -n "$data_dir" && -n "$key_uid" ]]; then
  if [[ ! -d "$data_dir" ]]; then
    warn "STATUS_DATA_DIR=$data_dir does not exist (stale path after migrating? copied the data dir?)"
  elif ! compgen -G "$data_dir/$key_uid*" >/dev/null; then
    warn "no files for STATUS_KEY_UID $key_uid in $data_dir — login will fail (wrong dir or key-uid?)"
  else
    ok "STATUS_DATA_DIR holds the account $key_uid"
  fi
fi

echo
if ((${#missing[@]})); then
  warn "missing in .env: ${missing[*]}"
  cat <<EOF

Next steps:
  1. Make sure the status-backend container is running (README)
  2. New bot account:   status-create-account --name Jelly --write-env $HERMES_HOME/.env
     (migrating instead? copy the old data dir + STATUS_* lines — see README)
  3. Add STATUS_ALLOW_FROM / STATUS_HOME_CHANNEL (your chat key) — see .env.example
  4. restart-hermes   then   journalctl -u hermes-gateway -f
EOF
else
  ok ".env has all required STATUS_* keys"
  echo "Start/restart with: restart-hermes   (logs: journalctl -u hermes-gateway -f)"
fi
