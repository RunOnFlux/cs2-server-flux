# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **Docker image repo**: the Counter-Strike 2 dedicated server for Flux, `runonflux/cs2-server-flux`.
steamcmd/steamcmd:ubuntu-24, Valve's native Linux server (app 730) and a bash supervisor. Same shape
as `~/work/tf2-server-flux` (read its CLAUDE.md: the pty, FIFO, exit-file and `script` patterns are the same).

## Commands

```bash
./tests/test-flux.sh
docker build -t cs2-server-flux:local .
./tests/test-image.sh
docker run --rm -v "$PWD:/mnt" -w /mnt koalaman/shellcheck:stable -x scripts/*.sh tests/*.sh tests/image/*.sh
```

## Things that will bite you

- **The install is ~69 GB** and local (`ml:`); only `cfg/` entries, CounterStrikeSharp's plugins/configs/shared and
  the ban lists live on the data volume, as links. Never make the install a synced mount.
- **gameinfo.gi is rewritten by every game update**: Metamod's `Game csgo/addons/metamod` line is put back on every start
  (`flux_gameinfo_metamod`). A server that dies twice early with the addons goes to safe mode.
- **A process substitution inside `$(...)` writes into the result**: the launcher's warnings go to a file
  (`/tmp/flux-launch.err`) and are logged after. tf2/gmod images still have the old form.
- **cs2 exits 127 after a clean quit** and, with CounterStrikeSharp loaded, can hang printing "stack smashing" after
  `Source2Shutdown`: the supervisor ends it 3 s after that line, and FLUX_STOP_GRACE is 8 s (Docker stops at 10).
- **`banid` wants `[U:1:n]`**, not a SteamID64 (it reads the number as a user id). Timed bans are never written by `writeid`.
- **The version line of `status` is the Steam login**: `[G:1:..]` valid token, `[A:1:..]` anonymous (no token),
  `[I:0:1]` + "disconnected from Steam3" = token not accepted. A2S does not flag `sv_password`.
- **Roster SteamIDs** come from the console log (`log on` + `sv_logecho 1` are launch args); `status` has none.
- `flux-console` reads the answer back from the console log; FLUX_CONSOLE_UNTIL waits for a closing line (`#end`).
- The game's own `gamemode_<mode>_server.cfg` files are one-liners we write: `exec flux/settings`, so the panel's
  settings run after each mode's own rules. `server.cfg` runs BEFORE them: round time etc. set there are overwritten.
- **Workshop maps download anonymously** (`host_workshop_map <id>` loads it; measured).
- Every user value is a `CS2_*` env, validated in `flux-launch.py`; a bad one is left out with a warning, never a crash.
