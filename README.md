# cs2-server-flux

The Counter-Strike 2 dedicated server for [Flux](https://runonflux.io), as
`runonflux/cs2-server-flux`. Valve's native 64-bit Linux server (Steam app 730, anonymous SteamCMD),
installed and updated on every start, under a supervisor (bash, PID 1) that:

- builds the launch line from the environment (`CS2_*`), every value validated
- runs the server in a pseudo-terminal fed by a FIFO, so its console reaches the logs and the hub
  (`flux-console`)
- keeps the owner's files on the data volume (`cfg/`, CounterStrikeSharp's plugins, configs and
  admins, the two ban lists) and links them into the install; everything else is Valve's, local to the node
- installs and updates **Metamod:Source 2.0** and **CounterStrikeSharp** (with its .NET runtime) when
  `CS2_ADDONS` is on, and **puts Metamod's line back into `gameinfo.gi` on every start**, because every
  game update writes that file away
- goes into **safe mode** (starts without the addons) when a server with them dies twice in a row
  within 3 minutes of starting, which is what a Valve update that Metamod/CounterStrikeSharp do not
  support yet looks like; it tries again after an addon update, the next game update, or
  `flux-cs2 addons-retry`
- backs up the owner's files on start (unless one is younger than an hour) and every 6 hours
- quits cleanly on stop (inside Docker's 10 s: the server is ended by force at 8 s) and restarts the
  server in place when it ends on its own, up to 5 times an hour, then exits 42 so the platform
  rebuilds or moves the container

CS2 has no world to save: everything the panel changes (settings, plugins, admins, bans) is written to
the data volume the moment it is made, so a stop or a kill loses nothing.

## Volumes

| Path | Flux mount | What |
|---|---|---|
| `/mnt/cs2/data` | `g:` (synced, small) | `cfg/` (`server.cfg`, `flux/settings.cfg`, `flux/mapcycle.txt`, plugin cfgs), `css/` (plugins, configs, admins, gamedata), `banned_user.cfg`, `banned_ip.cfg`, `flux/` (our log, backups, addon state) |
| `/mnt/cs2/server` | `ml:server` (local, never synced) | the game, **about 69 GB**, and the console logs (`flux-logs/`, 10 MB each, the newest 5) |

Spec: `g:/mnt/cs2/data|ml:server:/mnt/cs2/server`. Never put the server volume on a `g:` mount: Syncthing
would copy 69 GB.

## Port

`GAME_PORT` (27015): the game and the Steam query over UDP, RCON over TCP. SourceTV (27020) is not
served by this image.

## Environment

| Variable | | |
|---|---|---|
| `CS2_HOSTNAME` | `+hostname` | 63 characters, no double quotes or semicolons |
| `CS2_GAME_MODE` | `+game_type/+game_mode` | `competitive` (default), `casual`, `wingman`, `deathmatch`, `armsrace`, `custom` |
| `CS2_MAP` | `+map` | a map, or a Workshop map as an ID or link (default `de_dust2`) |
| `CS2_WORKSHOP_COLLECTION` | `+host_workshop_collection` | a Workshop collection's ID or link |
| `CS2_MAXPLAYERS` | `-maxplayers` | 2-64 (default 10). The owner's to raise to the game's own 64; hardware is the only limit |
| `CS2_PASSWORD` | `+sv_password` | no spaces or quotes |
| `CS2_RCON_PASSWORD` | `+rcon_password` | empty = RCON off |
| `CS2_GSLT` | `+sv_setsteamaccount` | the owner's own Game Server Login Token for App ID 730 (32 hex characters) |
| `CS2_ADDONS` | | `true` (default): Metamod + CounterStrikeSharp. `false`: off, plugins and configs are kept |
| `CS2_EXTRA_ARGS` | | appended (no port) |
| `SKIP_UPDATE` | | `true`: do not ask Steam for an update on start |

### The Steam token

Make one at <https://steamcommunity.com/dev/managegameservers> with App ID 730. Without one the server
**still runs** (it logs in anonymously, measured), but it is not in the Internet server list. A token Steam
does not accept (revoked, made for another game, used by another running server) leaves the server
disconnected from Steam: measured, `status` then prints `steamid : [I:0:1]` and "disconnected from Steam3".
`flux-cs2 status` reports all three as `gslt.state` (`missing` / `invalid` / `set`) and `gslt.login`
(`anonymous` / `ok` / `failed`) with a message for the panel.

## For the hub

`flux-console <command>` types one command into the server and prints the answer (`status`,
`kickid`, `changelevel`, `css_plugins list`, `meta list`...).

`flux-cs2` prints one JSON document per call:

| Command | |
|---|---|
| `status` | `state` (`updating` / `starting` / `running` / `restarting` / `stopped` / `needs-fix` / `crashed`), message, map, mode, players (A2S), build, `gslt`, addons, last backup |
| `players` | the roster: user id, name, bot, SteamID, ping, state, ip (from `status` + the log's connect lines) |
| `kick <userid> [reason]`, `ban <steamid\|ip> [minutes]`, `unban <steamid\|ip>` | bans are written to the data volume at once |
| `change-map <map\|workshop id\|link>` | `changelevel` or `host_workshop_map` |
| `settings` / `set-settings '<json>'` | the Server Settings form (34 settings): definitions and values; validated, written and applied live |
| `files` | stock maps, Workshop maps downloaded, map cycle, addons, plugins, the catalogue, admins, bans |
| `set-mapcycle '["de_dust2", ...]'` | `cfg/flux/mapcycle.txt` and the map group `mg_flux` |
| `plugin-add <id>` / `plugin-install <zip>` / `plugin-enable <name> on\|off` / `plugin-remove <name>` | CounterStrikeSharp plugins |
| `set-admins '<json>'` | `css/configs/admins.json`, merged |
| `addons-retry` | leave safe mode |
| `backups` / `restore <backup>` | |

`needs-fix` is a state the environment causes and a restart cannot cure (a `GAME_PORT` that is not a
port): the container waits for a redeploy instead of crash-looping.

`/tmp/flux-generation` holds `<generation> <epoch>`: a restart is over when it changes.

### Plugin catalogue (`scripts/plugins.json`)

Only plugins that load cleanly on the current build (CounterStrikeSharp 1.0.377, game 1.41.9.1): MatchZy,
Retakes (+ Instadefuse, Clutch Announce), Deathmatch, GG1 MapChooser. Retakes and Deathmatch conflict
(the catalogue says so).

## Tests

```bash
./tests/test-flux.sh                                   # unit tests, no Docker
docker build -t cs2-server-flux:local .
./tests/test-image.sh                                  # the real supervisor around a stand-in Steam and server
docker run --rm -v "$PWD:/mnt" -w /mnt koalaman/shellcheck:stable -x scripts/*.sh tests/*.sh tests/image/*.sh
```

## Measured (build 1.41.9.1)

- 69 GB installed (`csgo` 61 GB, community addons 6.4 GB); the download is 73 GB.
- Up in about 6 s on an installed volume (plus the SteamCMD check); idle (hibernating, nobody on) 1.08 GiB
  RAM, about 3 % of one core; 15 bots awake 1.30 GiB, about 24 % of one core.
- `docker stop` 3.5-4.5 s with six plugins loaded.

## Versions

- **1.0.0**: first release.
