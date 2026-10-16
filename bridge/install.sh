#!/usr/bin/env bash
# Idempotent user-level installer for the TinyPrint LE print bridge.
#
# Installs code under <prefix>/app, config + secrets under <config-dir>, a user systemd unit.
# Never uses sudo, never overwrites an existing bridge.env or token, never prints the token.
# Re-running updates the code tree only. Refuses (before changing anything) managed directories and
# files that are symlinks, foreign-owned or writable by group/others; never chmods existing dirs.
set -eu

usage() {
    cat <<'USAGE'
Usage: bridge/install.sh [options]

  --printer-mac MAC   printer LE public address (required on first install)
  --adapter-mac MAC   local Bluetooth adapter address (required on first install)
  --bind IP           listen address for a NEW bridge.env (default 127.0.0.1)
  --port N            listen port for a NEW bridge.env (default 8765)
  --prefix DIR        data directory (default ~/.local/share/tinyprint-bridge)
  --config-dir DIR    config + secrets directory (default ~/.config/tinyprint-bridge)
  --no-venv           do not create the virtualenv / install aiohttp (the unit needs an
                      existing <prefix>/venv, so combine with --no-systemd on a fresh install)
  --no-systemd        do not install or start the user unit
  -h, --help          show this help
USAGE
}

die() {
    printf 'install.sh: %s\n' "$1" >&2
    exit 2
}

printer_mac=""
adapter_mac=""
bind_addr="127.0.0.1"
port="8765"
prefix="${HOME}/.local/share/tinyprint-bridge"
config_dir="${HOME}/.config/tinyprint-bridge"
use_venv=1
use_systemd=1

while [ $# -gt 0 ]; do
    case "$1" in
        --printer-mac) [ $# -ge 2 ] || die "--printer-mac needs a value"; printer_mac=$2; shift 2 ;;
        --adapter-mac) [ $# -ge 2 ] || die "--adapter-mac needs a value"; adapter_mac=$2; shift 2 ;;
        --bind) [ $# -ge 2 ] || die "--bind needs a value"; bind_addr=$2; shift 2 ;;
        --port) [ $# -ge 2 ] || die "--port needs a value"; port=$2; shift 2 ;;
        --prefix) [ $# -ge 2 ] || die "--prefix needs a value"; prefix=$2; shift 2 ;;
        --config-dir) [ $# -ge 2 ] || die "--config-dir needs a value"; config_dir=$2; shift 2 ;;
        --no-venv) use_venv=0; shift ;;
        --no-systemd) use_systemd=0; shift ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done

mac_ok() {
    printf '%s' "$1" | grep -Eq '^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$'
}

env_file="${config_dir}/bridge.env"
token_file="${config_dir}/token"

# Validate everything before creating anything, so a bad call leaves no partial state.
if [ ! -f "$env_file" ]; then
    [ -n "$printer_mac" ] || die "--printer-mac is required on first install"
    [ -n "$adapter_mac" ] || die "--adapter-mac is required on first install"
fi
if [ -n "$printer_mac" ]; then mac_ok "$printer_mac" || die "--printer-mac must be a MAC like AA:BB:CC:DD:EE:FF"; fi
if [ -n "$adapter_mac" ]; then mac_ok "$adapter_mac" || die "--adapter-mac must be a MAC like AA:BB:CC:DD:EE:FF"; fi
[ -n "$bind_addr" ] || die "--bind must not be empty"
case "$bind_addr" in *[!0-9A-Fa-f:.]*) die "--bind must be an IP address" ;; esac
case "$port" in '' | *[!0-9]*) die "--port must be a number from 1 to 65535" ;; esac
{ [ "$port" -ge 1 ] && [ "$port" -le 65535 ]; } || die "--port must be a number from 1 to 65535"

# The directories end up in the systemd unit and in rm -rf/sed commands: absolute, boring paths only.
path_ok() {
    case "$1" in
        /* ) ;;
        * ) return 1 ;;
    esac
    case "$1" in
        *[!A-Za-z0-9_./+@:,=-]*) return 1 ;;
    esac
}
path_hint="an absolute path made of letters, digits and _./+@:,=- only; if this is the default, \$HOME contains other characters, so pass an explicit --prefix/--config-dir"
path_ok "$prefix" || die "--prefix must be ${path_hint}"
path_ok "$config_dir" || die "--config-dir must be ${path_hint}"

# The shared modules use Python 3.14 syntax (the version Home Assistant requires).
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 14) else 1)' >/dev/null 2>&1 \
    || die "Python 3.14 or newer is required (python3 on PATH is older or missing)"

# Same rules as the bridge at startup, so a value accepted here can never leave a unit that
# fails at once (and a bridge.env that the installer then refuses to repair).
python3 -c 'import ipaddress, sys; ipaddress.ip_address(sys.argv[1])' "$bind_addr" >/dev/null 2>&1 \
    || die "--bind must be an IP address"
for mac in "$printer_mac" "$adapter_mac"; do
    case "$mac" in 00:00:00:00:00:00) die "a MAC address must not be the null address" ;; esac
done

others_can_write() {
    [ -n "$(find "$1" -maxdepth 0 \( -perm -020 -o -perm -002 \) -print 2>/dev/null)" ]
}

# A managed directory that someone else can write to (or reach through a symlink) could have its
# marker or secrets planted, so it is refused rather than trusted. Missing ones are created 0700.
check_dir() {
    if [ ! -e "$1" ] && [ ! -L "$1" ]; then return 0; fi
    if [ -L "$1" ]; then die "$1 is a symlink; refusing to install through it"; fi
    if [ ! -d "$1" ]; then die "$1 exists but is not a directory"; fi
    if [ ! -O "$1" ]; then die "$1 is not owned by you (UID $(id -u)); refusing to install into it"; fi
    if others_can_write "$1"; then
        die "$1 is writable by group or others; refusing to trust it (tighten it yourself, e.g. chmod go-w; the installer never changes the mode of an existing directory)"
    fi
}

# Files are written with > and chmod-ed, both of which follow symlinks; STRICT=1 also refuses
# contents (secrets) that someone else could have written.
check_file() {
    if [ ! -e "$1" ] && [ ! -L "$1" ]; then return 0; fi
    if [ -L "$1" ]; then die "$1 is a symlink; refusing to follow it"; fi
    if [ ! -f "$1" ]; then die "$1 exists but is not a regular file"; fi
    if [ ! -O "$1" ]; then die "$1 is not owned by you (UID $(id -u)); refusing to use it"; fi
    if [ "$2" -eq 1 ] && others_can_write "$1"; then
        die "$1 is writable by group or others; refusing to trust its contents"
    fi
}

# The code tree is replaced with rm -rf, so only ever replace a tree this installer made, in
# directories nobody else controls.
for dir in "$prefix" "${prefix}/app" "$config_dir"; do
    check_dir "$dir"
done
check_file "$token_file" 1
check_file "$env_file" 1
app_marker="${prefix}/app/.tinyprint-bridge-app"
check_file "$app_marker" 0
unit_dir="${HOME}/.config/systemd/user"
unit_file="${unit_dir}/tinyprint-bridge.service"
if [ "$use_systemd" -eq 1 ]; then check_file "$unit_file" 0; fi
if { [ -e "${prefix}/app/bridge" ] || [ -e "${prefix}/app/custom_components" ]; } && [ ! -f "$app_marker" ]; then
    die "${prefix}/app exists but was not created by this installer; refusing to replace it (choose another --prefix)"
fi
# A unit that points at a missing interpreter would be installed and then fail at once.
if [ "$use_venv" -eq 0 ] && [ "$use_systemd" -eq 1 ] && [ ! -x "${prefix}/venv/bin/python" ]; then
    die "--no-venv needs an existing ${prefix}/venv (or add --no-systemd)"
fi

script_dir=$(cd "$(dirname "$0")" && pwd)
repo_root=$(cd "$script_dir/.." && pwd)
shared_src="${repo_root}/custom_components/tinyprint"
for module in protocol transport att bridge_wire; do
    [ -f "${shared_src}/${module}.py" ] || die "missing ${shared_src}/${module}.py (run from a full checkout)"
done

umask 077
# New directories come out 0700 through the umask; existing ones (possibly a broad prefix such as
# $HOME) are deliberately not chmod-ed.
mkdir -p "$prefix" "$config_dir"

# Code is replaced wholesale; secrets and config are never touched below this block.
app="${prefix}/app"
mkdir -p "$app"
# Re-check after creation: a directory that appeared since validation must pass the same rules.
for dir in "$prefix" "$app" "$config_dir"; do
    check_dir "$dir"
done
touch "$app_marker"
rm -rf "${app}/bridge" "${app}/custom_components"
mkdir -p "${app}/bridge" "${app}/custom_components/tinyprint"
cp -R "${script_dir}/tinyprint_bridge" "${app}/bridge/tinyprint_bridge"
find "${app}/bridge" -name __pycache__ -type d -prune -exec rm -rf {} +
for module in protocol transport att bridge_wire; do
    cp "${shared_src}/${module}.py" "${app}/custom_components/tinyprint/${module}.py"
done

# -s, not -f: an empty file left by a failed earlier run holds no secret to preserve.
if [ ! -s "$token_file" ]; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(48))' >"$token_file"
    chmod 600 "$token_file"
    token_state="generated"
else
    chmod 600 "$token_file"
    token_state="kept"
fi

if [ ! -f "$env_file" ]; then
    {
        printf 'TINYPRINT_BRIDGE_PRINTER_MAC=%s\n' "$printer_mac"
        printf 'TINYPRINT_BRIDGE_ADAPTER_MAC=%s\n' "$adapter_mac"
        printf 'TINYPRINT_BRIDGE_TOKEN_FILE=%s\n' "$token_file"
        printf 'TINYPRINT_BRIDGE_BIND=%s\n' "$bind_addr"
        printf 'TINYPRINT_BRIDGE_PORT=%s\n' "$port"
    } >"$env_file"
    chmod 600 "$env_file"
    env_state="created"
else
    chmod 600 "$env_file"
    env_state="kept"
    if [ -n "$printer_mac$adapter_mac" ] || [ "$bind_addr" != "127.0.0.1" ] || [ "$port" != "8765" ]; then
        printf 'Existing %s kept unchanged; edit it by hand to change addresses, bind or port.\n' "$env_file"
    fi
fi

if [ "$use_venv" -eq 1 ]; then
    [ -x "${prefix}/venv/bin/python" ] || python3 -m venv "${prefix}/venv"
    # Re-runs must work offline: only fetch aiohttp when it is missing.
    "${prefix}/venv/bin/python" -c 'import aiohttp' >/dev/null 2>&1 \
        || "${prefix}/venv/bin/python" -m pip install --quiet 'aiohttp>=3.9,<4'
fi

if [ "$use_systemd" -eq 1 ]; then
    mkdir -p "$unit_dir"
    # The shipped unit uses the default locations; rewrite them for custom directories.
    sed -e "s|%h/.local/share/tinyprint-bridge|${prefix}|g" \
        -e "s|%h/.config/tinyprint-bridge|${config_dir}|g" \
        "${script_dir}/tinyprint-bridge.service" >"$unit_file"
    chmod 644 "$unit_file"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user daemon-reload
        systemctl --user enable tinyprint-bridge.service
        systemctl --user restart tinyprint-bridge.service
    else
        printf 'systemctl not found; start the bridge manually (see docs/bridge.md).\n'
    fi
fi

printf 'TinyPrint bridge installed.\n'
printf '  code:   %s (replaced on every run)\n' "$app"
printf '  config: %s (%s)\n' "$env_file" "$env_state"
printf '  token:  %s (%s, mode 600)\n' "$token_file" "$token_state"
printf 'Next: read the token on this host with: cat %s\n' "$token_file"
printf 'Paste it into Home Assistant (TinyPrint options -> bridge token); do not put it in shell history.\n'
