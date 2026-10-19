#!/usr/bin/env python3
"""Prints the Counter-Strike 2 server's command line for the environment, shell-quoted, one line.

    flux-launch.py

The settings come from the CS2_* variables (the same names the Flux games hub writes) and
GAME_PORT. Every value is validated here; one that cannot be passed safely is left out and
reported on stderr, so the server still starts on the rest:

    CS2_HOSTNAME             +hostname "..."      no double quotes, 63 characters
    CS2_GAME_MODE            +game_type/+game_mode competitive (default), casual, wingman,
                                                  deathmatch, armsrace, custom
    CS2_MAP                  +map                 a map name (default de_dust2), or a Workshop map
                                                  as workshop/<id> -> +host_workshop_map <id>
    CS2_WORKSHOP_COLLECTION  +host_workshop_collection   a Workshop collection's ID: its maps are
                                                  downloaded and become the map group
    CS2_MAXPLAYERS           -maxplayers          2 to 64 (default 10)
    CS2_PASSWORD             +sv_password "..."   no spaces or quotes
    CS2_RCON_PASSWORD        +rcon_password "..." no spaces or quotes; empty = RCON off
    CS2_GSLT                 +sv_setsteamaccount  32 hex characters (a Game Server Login Token for
                                                  App ID 730)
    CS2_EXTRA_ARGS           appended as written (shell words), no -port / +port
    GAME_PORT                -port                the port the server binds (default 27015)

The map group: `mg_flux` when the panel has written a map cycle (FLUX_MAPCYCLE_FILE exists, the
group it defines in gamemodes_server.txt), else the game's mg_active. A Workshop collection is its
own map group.

Always written: `-dedicated`, `-console`, `-usercon` (RCON can be switched on by a password), the
ban lists, and `+log on +sv_logecho 1` (the connect/disconnect lines the roster reads SteamIDs from).
"""
import os
import re
import shlex
import sys

# game_type / game_mode per mode (game/csgo/gamemodes.txt). Retakes is a plugin's mode, on casual.
MODES = {
    'casual': ('0', '0'),
    'competitive': ('0', '1'),
    'wingman': ('0', '2'),
    'armsrace': ('1', '0'),
    'deathmatch': ('1', '2'),
    'custom': ('3', '0'),
}
DEFAULT_MODE = 'competitive'


def warn(message):
    print(f'WARN {message}', file=sys.stderr)


def env(name):
    return ' '.join(str(os.environ.get(name, '')).replace('\r', ' ').replace('\n', ' ').split())


def quoted(name, *, max_len=64, allow_spaces=False):
    value = env(name)
    if not value:
        return None
    if '"' in value or ';' in value or (not allow_spaces and ' ' in value) or len(value) > max_len:
        warn(f'{name} cannot be passed (quotes, semicolons{"" if allow_spaces else ", spaces"} or too long); left out')
        return None
    return f'"{value}"'


def workshop_id(value):
    m = re.search(r'[?&]id=(\d{4,20})', value) or re.fullmatch(r'workshop/(\d{4,20})', value) or re.fullmatch(r'(\d{4,20})', value)
    return m.group(1) if m else None


def build():
    args = ['./game/cs2.sh', '-dedicated', '-console', '-usercon']

    port = env('GAME_PORT') or '27015'
    if not (port.isdigit() and 1024 <= int(port) <= 65535):
        warn(f'GAME_PORT={port!r} is not a port; using 27015')
        port = '27015'
    args += ['-port', port]

    maxplayers = env('CS2_MAXPLAYERS') or '10'
    if not (maxplayers.isdigit() and 2 <= int(maxplayers) <= 64):
        warn(f'CS2_MAXPLAYERS={maxplayers!r} is not 2 to 64; using 10')
        maxplayers = '10'
    args += ['-maxplayers', maxplayers]
    # The ban lists: the server does not read them as it starts (measured: listid empty after a
    # restart with two bans in cfg/banned_user.cfg), so a ban written with writeid would be gone.
    args += ['+exec', 'banned_user.cfg', '+exec', 'banned_ip.cfg', '+log', 'on', '+sv_logecho', '1']

    mode = (env('CS2_GAME_MODE') or DEFAULT_MODE).lower()
    if mode not in MODES:
        warn(f'CS2_GAME_MODE={mode!r} is not one of {", ".join(MODES)}; using {DEFAULT_MODE}')
        mode = DEFAULT_MODE
    game_type, game_mode = MODES[mode]
    args += ['+game_type', game_type, '+game_mode', game_mode]

    collection = env('CS2_WORKSHOP_COLLECTION')
    collection_id = workshop_id(collection) if collection else None
    if collection and not collection_id:
        warn('CS2_WORKSHOP_COLLECTION is not a Workshop collection ID or link; left out')

    raw_map = env('CS2_MAP') or 'de_dust2'
    map_ws = workshop_id(raw_map) if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', raw_map) or raw_map.isdigit() else None
    if map_ws is None and not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', raw_map):
        warn(f'CS2_MAP={raw_map!r} is not a map name or a Workshop map; using de_dust2')
        raw_map = 'de_dust2'

    cycle = os.environ.get('FLUX_MAPCYCLE_FILE', '/mnt/cs2/data/cfg/flux/mapcycle.txt')
    if collection_id:
        args += ['+host_workshop_collection', collection_id]
    else:
        args += ['+mapgroup', 'mg_flux' if os.path.isfile(cycle) else 'mg_active']
    if map_ws:
        args += ['+host_workshop_map', map_ws]
    elif not collection_id or env('CS2_MAP'):
        args += ['+map', raw_map]

    hostname = quoted('CS2_HOSTNAME', max_len=63, allow_spaces=True)
    if hostname:
        args += ['+hostname', hostname]
    password = quoted('CS2_PASSWORD')
    if password:
        args += ['+sv_password', password]
    rcon = quoted('CS2_RCON_PASSWORD')
    if rcon:
        args += ['+rcon_password', rcon]
    else:
        warn('no CS2_RCON_PASSWORD: RCON stays off')

    gslt = env('CS2_GSLT')
    if gslt:
        if re.fullmatch(r'[0-9A-Fa-f]{32}', gslt):
            args += ['+sv_setsteamaccount', gslt]
        else:
            warn('CS2_GSLT is not a 32-character token; left out')
    else:
        warn('no CS2_GSLT: the server cannot log into Steam')

    extra = env('CS2_EXTRA_ARGS')
    if extra:
        try:
            words = shlex.split(extra)
        except ValueError:
            warn('CS2_EXTRA_ARGS has unbalanced quotes; left out')
            words = []
        if any(w.lower() in ('-port', '+port', '+hostport') for w in words):
            warn('CS2_EXTRA_ARGS sets the port, which GAME_PORT owns; left out')
            words = []
        args += words
    return args


def main():
    print(' '.join(shlex.quote(a) for a in build()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
