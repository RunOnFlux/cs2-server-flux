#!/bin/bash
# Unit tests: the pure decisions in flux-lib.sh, the launch line and fluxcs2.py. No Docker, no
# Steam, no game. Run from the repository root.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

# shellcheck source=scripts/flux-lib.sh
FLUX_LOG="" source scripts/flux-lib.sh

pass=0
fail=0
check() { # description, expected, actual
  if [ "$2" = "$3" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'FAIL %s\n  expected: [%s]\n  actual:   [%s]\n' "$1" "$2" "$3"
  fi
}
status() { "$@" >/dev/null 2>&1; echo $?; }
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
# launch VAR=value...: the launch line with exactly these variables (warnings on stderr dropped).
launch() { env -i PATH="${PATH}" "$@" python3 scripts/flux-launch.py 2>/dev/null; }
warnings() { { env -i PATH="${PATH}" "$@" python3 scripts/flux-launch.py >/dev/null; } 2>&1; }

# --- Inputs ------------------------------------------------------------------------------------
check "27015 is a port" "27015" "$(flux_valid_port 27015)"
check "port 80 is refused" "1" "$(status flux_valid_port 80)"
check "a word is not a port" "1" "$(status flux_valid_port abc)"
check "old restarts age out" "900 950" "$(flux_restarts_in_window 1000 200 100 700 900 950)"
check "a requested quit is not counted" "1" "$(status flux_restart_counts 'requested: the server quit (exit 0)')"
check "a crash is counted" "0" "$(status flux_restart_counts 'the server ended on its own (exit 139)')"
check "keeps the newest" $'c\nd' "$(flux_files_to_prune 2 a b c d)"
check "the stop grace fits Docker's 10 s" "0" "$(status grep -q 'FLUX_STOP_GRACE:-8' scripts/flux-entrypoint.sh)"

# --- Safe mode ---------------------------------------------------------------------------------
check "an early crash with the addons counts" "0" "$(status flux_is_early_addon_crash 20 139 1 0 180)"
check "a late crash does not" "1" "$(status flux_is_early_addon_crash 4000 139 1 0 180)"
check "without the addons it is not theirs" "1" "$(status flux_is_early_addon_crash 20 139 0 0 180)"
check "a requested restart is not a crash" "1" "$(status flux_is_early_addon_crash 20 0 1 1 180)"

# --- gameinfo.gi: Metamod's line put back after every game update -------------------------------
gi="${tmp}/gameinfo.gi"
printf '"GameInfo"\n{\n\tFileSystem\n\t{\n\t\tSearchPaths\n\t\t{\n\t\t\tGame_LowViolence\tcsgo_lv\n\t\t\tGame\tcsgo\n\t\t\tGame\tcore\n\t\t}\n\t}\n}\n' >"${gi}"
check "no Metamod line in a fresh file" "1" "$(status flux_gameinfo_has_metamod "${gi}")"
check "the patch works" "0" "$(status flux_gameinfo_metamod "${gi}" on)"
check "Metamod is first after Game_LowViolence" "Game	csgo/addons/metamod" "$(sed -n '/Game_LowViolence/{n;s/^[[:space:]]*//;p}' "${gi}")"
flux_gameinfo_metamod "${gi}" on
check "patching twice adds one line" "1" "$(grep -c 'addons/metamod' "${gi}")"
check "taking it out" "0" "$(flux_gameinfo_metamod "${gi}" off; grep -c 'addons/metamod' "${gi}")"
printf 'nothing to anchor on\n' >"${tmp}/odd.gi"
check "a file without the anchor is refused" "1" "$(status flux_gameinfo_metamod "${tmp}/odd.gi" on)"
check "a missing file is refused" "1" "$(status flux_gameinfo_metamod "${tmp}/none.gi" on)"

# --- The launch line ---------------------------------------------------------------------------
base="./game/cs2.sh -dedicated -console -usercon -port 27015 -maxplayers 10 +exec banned_user.cfg +exec banned_ip.cfg +log on +sv_logecho 1"
check "defaults" "${base} +game_type 0 +game_mode 1 +mapgroup mg_active +map de_dust2" "$(launch)"
check "game modes map to game_type/game_mode" "+game_type 1 +game_mode 2" "$(launch CS2_GAME_MODE=deathmatch | grep -o '+game_type . +game_mode .')"
check "wingman" "+game_type 0 +game_mode 2" "$(launch CS2_GAME_MODE=Wingman | grep -o '+game_type . +game_mode .')"
check "an unknown mode falls back to competitive" "+game_type 0 +game_mode 1" "$(launch CS2_GAME_MODE=zombies | grep -o '+game_type . +game_mode .')"
check "everything set" \
  "./game/cs2.sh -dedicated -console -usercon -port 31015 -maxplayers 64 +exec banned_user.cfg +exec banned_ip.cfg +log on +sv_logecho 1 +game_type 0 +game_mode 0 +mapgroup mg_active +map de_inferno +hostname '\"My CS2 Server\"' +sv_password '\"pw\"' +rcon_password '\"rcon12345\"' +sv_setsteamaccount 0123456789ABCDEF0123456789ABCDEF +sv_region 3" \
  "$(launch GAME_PORT=31015 CS2_MAXPLAYERS=64 CS2_GAME_MODE=casual CS2_MAP=de_inferno CS2_HOSTNAME='My CS2 Server' CS2_PASSWORD=pw CS2_RCON_PASSWORD=rcon12345 CS2_GSLT=0123456789ABCDEF0123456789ABCDEF CS2_EXTRA_ARGS='+sv_region 3')"
check "the owner may raise players to the game's own 64" "0" "$(status grep -q -- '-maxplayers 64' <<<"$(launch CS2_MAXPLAYERS=64)")"
check "a Workshop map by id" "0" "$(status grep -q '+host_workshop_map 3816178873' <<<"$(launch CS2_MAP=3816178873)")"
check "a Workshop map by link" "0" "$(status grep -q '+host_workshop_map 3816178873' <<<"$(launch CS2_MAP='https://steamcommunity.com/sharedfiles/filedetails/?id=3816178873')")"
check "a Workshop collection" "0" "$(status grep -q '+host_workshop_collection 123456789' <<<"$(launch CS2_WORKSHOP_COLLECTION=123456789)")"
check "a collection alone starts on its own maps, not on a +map" "1" "$(status grep -q -- '+map' <<<"$(launch CS2_WORKSHOP_COLLECTION=123456789)")"
check "a collection with a start map" "0" "$(status grep -q -- '+map de_nuke' <<<"$(launch CS2_WORKSHOP_COLLECTION=123456789 CS2_MAP=de_nuke)")"
check "a hostname with a quote is left out" "1" "$(status grep -q hostname <<<"$(launch CS2_HOSTNAME='bad"name')")"
check "a password with a space is left out" "1" "$(status grep -q sv_password <<<"$(launch CS2_PASSWORD='a b')")"
check "a map that is not one word falls back" "0" "$(status grep -q '+map de_dust2' <<<"$(launch CS2_MAP='de_dust2; quit')")"
check "players out of range fall back" "0" "$(status grep -q -- '-maxplayers 10' <<<"$(launch CS2_MAXPLAYERS=500)")"
check "a bad port falls back" "0" "$(status grep -q -- '-port 27015' <<<"$(launch GAME_PORT=80)")"
check "a token of the wrong shape is left out" "1" "$(status grep -q sv_setsteamaccount <<<"$(launch CS2_GSLT=not-a-token)")"
check "and said" "0" "$(status grep -q 'CS2_GSLT is not a 32' <<<"$(warnings CS2_GSLT=nope)")"
check "no token is said" "0" "$(status grep -q 'no CS2_GSLT' <<<"$(warnings)")"
check "extra args may not set the port" "1" "$(status grep -q 'sv_lan' <<<"$(launch CS2_EXTRA_ARGS='+sv_lan 0 -port 1')")"
# shellcheck disable=SC2016  # the $(...) is the payload, on purpose not expanded
check "a shell metacharacter stays an argument" "0" "$(status grep -qF "'\$(reboot)'" <<<"$(launch CS2_EXTRA_ARGS='$(reboot)')")"
check "no RCON password is said" "0" "$(status grep -q 'RCON stays off' <<<"$(warnings)")"

# --- fluxcs2.py --------------------------------------------------------------------------------
check "fluxcs2.py unit tests" "0" "$(status python3 -I -W ignore tests/test_fluxcs2.py)"

printf '%s passed, %s failed\n' "${pass}" "${fail}"
[ "${fail}" = "0" ]
