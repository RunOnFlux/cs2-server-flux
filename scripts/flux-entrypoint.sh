#!/bin/bash
# PID 1, and the supervisor of the Counter-Strike 2 server.
#
# The game runs as a CHILD, never exec, so a stop reaches us first, and a server that ends on its
# own is followed by the next one in place:
#
#   - every start: SteamCMD update (the game is ~60 GB, on the local install volume), the owner's
#     files linked from the data volume (cfg/, the CounterStrikeSharp plugins, configs and admins,
#     the ban lists), Metamod:Source + CounterStrikeSharp installed or updated when CS2_ADDONS is
#     on and gameinfo.gi patched to load them (every game update writes that file away), the map
#     group from the panel's map cycle, the launch line from the environment (flux-launch.py), then
#     the server in a PSEUDO-TERMINAL fed by a FIFO: its console reaches the container's output, a
#     rotated log on the install volume, and flux-console
#   - a backup of the owner's files on start, unless one is younger than an hour, and every
#     FLUX_BACKUP_HOURS; a restore the hub asked for (flux-cs2 restore) is applied between two
#     servers, after a safety backup
#   - SAFE MODE: a server with the addons on that dies early twice in a row (a CS2 update that
#     Metamod or CounterStrikeSharp do not support yet) starts without them until they or the game
#     change, or the owner asks again (flux-cs2 addons-retry)
#   - SIGTERM/SIGINT from Docker: `quit` on the console
#   - the server ending on its own: a new generation in the same container, updated first
#   - FLUX_RESTART_MAX_ATTEMPTS unplanned restarts inside FLUX_RESTART_WINDOW seconds: the
#     container ends with 42, a server that cannot stay up is the platform's to rebuild or move
#
# Same shape as runonflux/tf2-server-flux.

set -uo pipefail
# shellcheck source=scripts/flux-lib.sh
source /opt/flux/flux-lib.sh

FLUX_RESTART_MAX_ATTEMPTS="${FLUX_RESTART_MAX_ATTEMPTS:-5}"
FLUX_RESTART_WINDOW="${FLUX_RESTART_WINDOW:-3600}"
FLUX_RESTART_BACKOFF="${FLUX_RESTART_BACKOFF:-10}"
# Docker (and FluxOS) kill the container 10 s after SIGTERM: the server is ended by force at 8 s.
# Nothing is lost: CS2 has no world to save, and every panel change is written to the data volume
# the moment it is made (settings, plugins, admins, bans with writeid).
FLUX_STOP_GRACE="${FLUX_STOP_GRACE:-8}"
# Seconds between the server's "Source2Shutdown" and the end of the process before it is ended.
FLUX_QUIT_GRACE="${FLUX_QUIT_GRACE:-3}"
FLUX_BACKUP_HOURS="${FLUX_BACKUP_HOURS:-6}"
# A server with the addons that ends on its own sooner than this counts towards safe mode.
FLUX_EARLY_CRASH_SECONDS="${FLUX_EARLY_CRASH_SECONDS:-180}"
FLUX_STEAMCLIENT="${FLUX_STEAMCLIENT:-/root/.local/share/Steam/steamcmd/linux64/steamclient.so}"
cs2() { python3 /opt/flux/fluxcs2.py "$@"; }

s="${FLUX_SERVER_DIR}"
g="${FLUX_GAME_DIR}"
d="${FLUX_DATA_DIR}"
addons_state="${d}/flux/addons.json"
safe_mode="${d}/flux/safe-mode"

rm -f "${FLUX_RESTART_MARKER}"
mkdir -p "${s}" "${d}/flux" "$(dirname "${FLUX_LOG}")" "${FLUX_GAME_LOG_DIR}"
# An older image kept flux.log on the synced data volume: move it here, once.
flux_log_migrate "${FLUX_DATA_DIR}/flux/flux.log"
flux_banner "Counter-Strike 2"

port="$(flux_valid_port "${GAME_PORT:-27015}")" || port=""
export GAME_PORT="${port:-${GAME_PORT:-27015}}"
rm -f "${FLUX_STATE_FILE}"
flux_log "flux-entrypoint starting (image ${FLUX_IMAGE_VERSION:-dev}, port ${GAME_PORT})"

game_pid=""
backup_pid=""
ready_pid=""
game_rc=0
terminating=0
generation=0
restart_history=()
early_crashes=0
addons_loaded=0
started_at=0

rm -f "${FLUX_CONSOLE_FIFO}"
mkfifo -m 600 "${FLUX_CONSOLE_FIFO}"
exec 3<>"${FLUX_CONSOLE_FIFO}"

stop_server() {
  [ -n "${game_pid}" ] || return 0
  if ! printf 'quit\n' >&3; then
    flux_log "WARN the console did not take the command; stopping the server with a signal"
    pkill -TERM -f "${FLUX_GAME_PROCESS}" 2>/dev/null
  fi
}

# shellcheck disable=SC2329  # invoked by the trap below
term_handler() {
  terminating=1
  flux_log "stop requested ($1): quitting the server, no restart will follow"
  stop_server
}
trap 'term_handler TERM' TERM
trap 'term_handler INT' INT

update_server() {
  local attempt validate steam_pid steam_rc
  cs2 state updating "Checking Steam for a Counter-Strike 2 update" >/dev/null 2>&1
  if flux_truthy "${SKIP_UPDATE:-}" && [ -x "${s}/game/cs2.sh" ]; then
    flux_log "SKIP_UPDATE is set: not updating the server files"
    return 0
  fi
  if [ -x "${s}/game/cs2.sh" ]; then
    flux_log "checking Steam for a Counter-Strike 2 update (app ${FLUX_STEAM_APP})"
  else
    flux_log "installing Counter-Strike 2 from Steam (app ${FLUX_STEAM_APP}): about 60 GB, the first start takes a while"
    cs2 state updating "Downloading Counter-Strike 2 from Steam (about 70 GB): the first start takes a while" >/dev/null 2>&1
  fi
  # No `validate` on the first attempt: it re-hashes the whole ~60 GB install. A broken install
  # fails the first attempt, and every retry validates. app_info_update first: a fresh SteamCMD
  # otherwise answers "Missing configuration" on the first app_update.
  validate=""
  for attempt in 1 2 3 4 5; do
    # In the background and waited for: a `docker stop` during a download reaches the trap at
    # once instead of after SteamCMD (a foreground child holds bash's traps until it ends).
    # shellcheck disable=SC2086  # validate is "" or "validate"
    steamcmd +force_install_dir "${s}" +login anonymous +app_info_update 1 +app_update "${FLUX_STEAM_APP}" ${validate} +quit >/tmp/steamcmd.log 2>&1 &
    steam_pid=$!
    wait "${steam_pid}"
    steam_rc=$?
    if [ "${terminating}" = "1" ]; then
      kill "${steam_pid}" 2>/dev/null
      return 1
    fi
    # "Success! App '730' fully installed." or "... already up to date.": SteamCMD's exit code
    # alone is 0 on some failures (measured on other games), so the line is what counts.
    if [ "${steam_rc}" = "0" ] && grep -q "Success! App '${FLUX_STEAM_APP}'" /tmp/steamcmd.log; then
      return 0
    fi
    validate="validate"
    flux_log "WARN steamcmd failed (attempt ${attempt}/5, exit ${steam_rc}): $(grep -E 'ERROR|Error|error|Missing|Failed' /tmp/steamcmd.log | tail -1 | tr -d '\r')"
    sleep 5
  done
  if [ -x "${s}/game/cs2.sh" ]; then
    flux_log "WARN could not update from Steam; starting the build already installed"
    return 0
  fi
  flux_log "ERROR could not install the server from Steam"
  return 1
}

# $1 = a data path, $2 = the install path that must be a link to it. A real folder or file found
# there first (the game's or an addon's own) is merged into the data, never overwriting it.
link_into() {
  local src="$1" target="$2"
  if [ -L "${target}" ]; then
    [ "$(readlink "${target}")" = "${src}" ] && return 0
    rm -f "${target}"
  elif [ -d "${target}" ]; then
    mkdir -p "${src}" && cp -an "${target}/." "${src}/" 2>/dev/null
    rm -rf "${target}"
  elif [ -e "${target}" ]; then
    [ -e "${src}" ] || cp -a "${target}" "${src}"
    rm -f "${target}"
  fi
  mkdir -p "$(dirname "${target}")"
  ln -s "${src}" "${target}"
}

# The owner's part of csgo/ lives on the data volume:
#   cfg/<anything>   each entry linked into csgo/cfg (server.cfg, flux/ with the panel's settings
#                    and map cycle, a plugin's cfg folder like MatchZy/, an owner's own
#                    gamemode_*_server.cfg). The game's own cfgs stay the install's.
#   css/plugins, css/configs, css/shared   CounterStrikeSharp's, linked into its addon folder
#   banned_user.cfg, banned_ip.cfg         the ban lists, linked into csgo/cfg
link_data() {
  local entry name mode
  mkdir -p "${d}/cfg/flux" "${d}/css/plugins" "${d}/css/configs" "${d}/css/shared" "${d}/css/gamedata"
  [ -f "${d}/cfg/server.cfg" ] || cp /opt/flux/server.cfg "${d}/cfg/server.cfg"
  [ -f "${d}/cfg/flux/settings.cfg" ] || cp /opt/flux/settings.cfg "${d}/cfg/flux/settings.cfg"
  for entry in "${d}"/cfg/* "${d}"/cfg/.[!.]*; do
    [ -e "${entry}" ] || continue
    link_into "${entry}" "${g}/cfg/$(basename "${entry}")"
  done
  # The panel's settings run after each mode's own rules: gamemode_<mode>.cfg sets the mode's
  # round time, money and bots, then the game execs gamemode_<mode>_server.cfg, which is this
  # one-liner (unless the owner keeps their own in cfg/, linked above).
  for mode in casual competitive competitive2v2 deathmatch armsrace custom retakecasual demolition; do
    name="${g}/cfg/gamemode_${mode}_server.cfg"
    [ -L "${name}" ] && continue
    printf '// Written by runonflux/cs2-server-flux on every start: the panel'"'"'s Server Settings, after the mode'"'"'s own.\n// Your own version of this file goes in cfg/ on the data volume.\nexec flux/settings\n' >"${name}"
  done
  for name in banned_user.cfg banned_ip.cfg; do
    touch "${d}/${name}"
    link_into "${d}/${name}" "${g}/cfg/${name}"
  done
}

# Links CounterStrikeSharp's owner folders into the addon (on the install volume).
link_css() {
  local base="${g}/addons/counterstrikesharp" name
  [ -d "${base}" ] || return 0
  for name in plugins configs shared; do
    link_into "${d}/css/${name}" "${base}/${name}"
  done
  # Plugins' own gamedata (MatchZy's): copied in, the framework's gamedata.json is never touched.
  if [ -d "${base}/gamedata" ]; then
    find "${d}/css/gamedata" -maxdepth 1 -name '*.json' ! -name gamedata.json -exec cp -f {} "${base}/gamedata/" \; 2>/dev/null
  fi
}

# $1 = URL. The JSON field `tag_name` of a GitHub "latest release" answer, and the download URL of
# the asset whose name matches $2. Printed as "<tag> <url>".
github_asset() {
  curl -fsSL --max-time 30 -H 'Accept: application/vnd.github+json' "https://api.github.com/repos/$1/releases/latest" 2>/dev/null \
    | python3 -c 'import json,re,sys
d=json.load(sys.stdin)
for a in d.get("assets", []):
    if re.search(sys.argv[1], a.get("name", "")):
        print(d.get("tag_name"), a["browser_download_url"]); break' "$2" 2>/dev/null
}

# CS2_ADDONS on: Metamod:Source (2.0, the branch for CS2) and CounterStrikeSharp (its latest
# release, WITH its .NET runtime), installed into csgo/addons on the install volume and updated
# when a new build is out. Plugins, their configs and the admins are the owner's, on the data
# volume (link_css). The versions installed are in flux/addons.json.
install_addons() {
  local tmp name tag url mm_now css_now changed=0
  mkdir -p "${g}/addons"
  mm_now="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("metamod") or "")' "${addons_state}" 2>/dev/null)"
  css_now="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("css") or "")' "${addons_state}" 2>/dev/null)"
  tmp="$(mktemp -d)"

  name="$(curl -fsSL --max-time 30 "https://mms.alliedmods.net/mmsdrop/${FLUX_MMS_BRANCH:-2.0}/mmsource-latest-linux" 2>/dev/null | tr -d '\r\n')"
  if [ -n "${name}" ] && { [ "${name}" != "${mm_now}" ] || [ ! -f "${g}/addons/metamod/bin/linuxsteamrt64/metamod.2.cs2.so" ]; }; then
    if curl -fsSL --max-time 300 -o "${tmp}/${name}" "https://mms.alliedmods.net/mmsdrop/${FLUX_MMS_BRANCH:-2.0}/${name}" \
      && mkdir -p "${tmp}/mm" && tar -C "${tmp}/mm" -xzf "${tmp}/${name}"; then
      # metaplugins.ini is the owner's if they made one; the rest is Metamod's own.
      [ -f "${g}/addons/metamod/metaplugins.ini" ] && rm -f "${tmp}/mm/addons/metamod/metaplugins.ini"
      cp -a "${tmp}/mm/addons/." "${g}/addons/" && mm_now="${name}" && changed=1
      flux_log "addons: Metamod:Source ${name}"
    else
      flux_log "WARN could not download Metamod:Source (${name}); keeping what is installed"
    fi
  elif [ -z "${name}" ]; then
    flux_log "WARN could not ask AlliedModders for the latest Metamod:Source; keeping what is installed"
  fi

  read -r tag url <<<"$(github_asset roflmuffin/CounterStrikeSharp '^counterstrikesharp-with-runtime-linux-[0-9.]+\.zip$')"
  if [ -n "${tag:-}" ] && { [ "${tag}" != "${css_now}" ] || [ ! -d "${g}/addons/counterstrikesharp/bin" ]; }; then
    if curl -fsSL --max-time 600 -o "${tmp}/css.zip" "${url}" && mkdir -p "${tmp}/css" && unzip -q -o "${tmp}/css.zip" -d "${tmp}/css"; then
      # The owner's folders are links into the data volume: the archive's copies (example
      # configs, an empty plugins folder) go there without overwriting anything.
      local part
      for part in plugins configs shared; do
        if [ -d "${tmp}/css/addons/counterstrikesharp/${part}" ]; then
          mkdir -p "${d}/css/${part}"
          cp -an "${tmp}/css/addons/counterstrikesharp/${part}/." "${d}/css/${part}/" 2>/dev/null
          rm -rf "${tmp}/css/addons/counterstrikesharp/${part}"
        fi
      done
      cp -a "${tmp}/css/addons/." "${g}/addons/" && css_now="${tag}" && changed=1
      flux_log "addons: CounterStrikeSharp ${tag}"
    else
      flux_log "WARN could not download CounterStrikeSharp ${tag}; keeping what is installed"
    fi
  elif [ -z "${tag:-}" ]; then
    flux_log "WARN could not ask GitHub for the latest CounterStrikeSharp; keeping what is installed"
  fi
  rm -rf "${tmp}"
  python3 -c 'import json,sys; json.dump({"metamod": sys.argv[2] or None, "css": sys.argv[3] or None, "loaded": sys.argv[4] == "1", "build": sys.argv[5] or None}, open(sys.argv[1], "w"))' \
    "${addons_state}" "${mm_now}" "${css_now}" 0 "${build:-}"
  # New addon builds are what fixes a game update: safe mode is left to try them.
  if [ "${changed}" = "1" ] && [ -f "${safe_mode}" ]; then
    rm -f "${safe_mode}"
    flux_log "addons: a new build is installed; leaving safe mode to try it"
  fi
  link_css
}

# The addons load from gameinfo.gi, which the game's updates replace: patched on every start.
prepare_addons() {
  addons_loaded=0
  if ! flux_truthy "${CS2_ADDONS:-true}"; then
    flux_gameinfo_metamod "${g}/gameinfo.gi" off
    flux_log "addons: off (CS2_ADDONS=false); plugins and configs are kept"
    return 0
  fi
  install_addons
  if [ -f "${safe_mode}" ]; then
    local why
    why="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("reason",""))' "${safe_mode}" 2>/dev/null)"
    # A different game build since safe mode began may be the one the addons support again.
    if [ -n "${build:-}" ] && ! grep -q "\"build\": \"${build}\"" "${safe_mode}"; then
      rm -f "${safe_mode}"
      flux_log "addons: the game was updated since safe mode began; trying the addons again"
    else
      flux_gameinfo_metamod "${g}/gameinfo.gi" off
      flux_log "addons: SAFE MODE, starting without Metamod and CounterStrikeSharp (${why}). They load again after their next update, the next game update, or Try Again in the panel."
      return 0
    fi
  fi
  if [ ! -f "${g}/addons/metamod/bin/linuxsteamrt64/metamod.2.cs2.so" ]; then
    flux_gameinfo_metamod "${g}/gameinfo.gi" off
    flux_log "WARN addons: Metamod:Source is not installed; starting without the addons"
    return 0
  fi
  if flux_gameinfo_metamod "${g}/gameinfo.gi" on; then
    addons_loaded=1
    python3 -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["loaded"]=True; json.dump(d, open(p, "w"))' "${addons_state}" 2>/dev/null
    flux_log "addons: Metamod:Source and CounterStrikeSharp load with this start"
  else
    flux_log "WARN addons: could not patch gameinfo.gi; starting without the addons"
  fi
}

# shellcheck disable=SC2329  # run in the background below
backupper() {
  [ "${FLUX_BACKUP_HOURS}" -gt 0 ] 2>/dev/null || exit 0
  while true; do
    sleep $((FLUX_BACKUP_HOURS * 3600))
    flux_log "backup (every ${FLUX_BACKUP_HOURS}h): $(cs2 backup "every ${FLUX_BACKUP_HOURS}h" force 2>&1 | tail -c 300)"
  done
}

# The console, line by line: carriage returns and terminal escapes dropped, onto the container's
# output and into the current log file, which is rotated at FLUX_GAME_LOG_MAX_BYTES.
# shellcheck disable=SC2329  # used in a process substitution below
console_writer() {
  local line file bytes=0
  file="${FLUX_GAME_LOG_DIR}/$(date +%Y%m%d-%H%M%S)-console.log"
  : >"${file}"
  while IFS= read -r line; do
    line="${line//$'\r'/}"
    # shellcheck disable=SC2001  # a regex, not a fixed string
    line="$(sed 's/\x1b\[[0-9;?]*[A-Za-z]//g' <<<"${line}")"
    printf '%s\n' "${line}"
    printf '%s\n' "${line}" >>"${file}"
    # The server has finished quitting: everything after this is its shutdown, which with
    # CounterStrikeSharp loaded can hang forever printing "*** stack smashing detected ***"
    # (measured, CSS 1.0.376). wait_for_generation ends it FLUX_QUIT_GRACE seconds later.
    case "${line}" in
      Source2Shutdown* | "MainLoop returning"*) [ -f "${FLUX_QUITTING_FILE}" ] || date -u +%s >"${FLUX_QUITTING_FILE}" ;;
    esac
    bytes=$((bytes + ${#line} + 1))
    if [ "${bytes}" -ge "${FLUX_GAME_LOG_MAX_BYTES}" ]; then
      file="${FLUX_GAME_LOG_DIR}/$(date +%Y%m%d-%H%M%S)-console.log"
      : >"${file}"
      bytes=0
      flux_prune "${FLUX_GAME_LOG_DIR}" '*-console.log' "${FLUX_GAME_LOG_KEEP}"
    fi
  done
}

# running = the server answers the Steam query on its port (also while it hibernates empty).
# shellcheck disable=SC2329  # run in the background below
ready_watcher() {
  local i
  for i in $(seq 1 900); do
    sleep 2
    if cs2 probe >/dev/null 2>&1; then
      cs2 state running >/dev/null 2>&1
      flux_log "the server answers on port ${GAME_PORT}: running"
      return 0
    fi
  done
  flux_log "WARN the server has not answered the Steam query on port ${GAME_PORT} in 30 minutes"
}

start_generation() {
  local launch line
  generation=$((generation + 1))
  rm -f "${FLUX_RESTART_MARKER}" "${FLUX_EXIT_FILE}" "${FLUX_QUITTING_FILE}"
  if [ -z "${port}" ]; then
    flux_log "ERROR GAME_PORT='${GAME_PORT:-}' is not a port from 1024 to 65535"
    cs2 state needs-fix "GAME_PORT is not a port from 1024 to 65535. The app's ports must be set by the deployment; redeploy the app." >/dev/null 2>&1
    return 3
  fi

  if ! update_server; then
    [ "${terminating}" = "1" ] && return 2
    return 1
  fi
  [ "${terminating}" = "1" ] && return 2

  # The build, for the panel's Overview: steam.inf's PatchVersion (1.41.9.1) and ServerVersion.
  build="$(sed -n 's/^PatchVersion=//p' "${g}/steam.inf" 2>/dev/null | tr -dc '0-9.')"
  [ -n "${build}" ] && flux_log "Counter-Strike 2 build ${build} (server version $(sed -n 's/^ServerVersion=//p' "${g}/steam.inf" | tr -dc '0-9'))"
  cs2 state starting "Preparing the server's files and plugins" >/dev/null 2>&1
  # The game finds Steam's client library here (SteamCMD ships it); without it no Steam login.
  mkdir -p "${HOME}/.steam/sdk64" && ln -sf "${FLUX_STEAMCLIENT}" "${HOME}/.steam/sdk64/steamclient.so"
  # A restore the hub asked for (flux-cs2 restore): applied before anything reads the files.
  [ -f "${d}/flux/restore-request" ] && flux_log "restore: $(cs2 apply-restore 2>&1 | tail -c 400)"
  link_data
  flux_log "backup (start): $(cs2 backup start 2>&1 | tail -c 300)"
  prepare_addons
  cs2 write-mapgroup >/dev/null 2>&1 || flux_log "WARN could not write the map group"
  case "$(cs2 gslt-state 2>/dev/null)" in
    missing) flux_log "WARN no Steam Game Server Login Token (CS2_GSLT): the server runs, but it is not logged into Steam, so it is not in the Internet server list. Make a token at steamcommunity.com/dev/managegameservers with App ID 730" ;;
    invalid) flux_log "WARN CS2_GSLT is not a 32-character token (letters A-F and digits): left out, the server is not logged into Steam" ;;
  esac

  # The launcher's warnings go to a file and are logged after: a process substitution here would
  # write its log lines into the command substitution, i.e. into the launch line.
  launch="$(python3 /opt/flux/flux-launch.py 2>/tmp/flux-launch.err)"
  while IFS= read -r line; do flux_log "launch: ${line}"; done </tmp/flux-launch.err
  if [ -z "${launch}" ]; then
    flux_log "ERROR could not build the launch line"
    return 1
  fi
  flux_prune "${FLUX_GAME_LOG_DIR}" '*-console.log' "${FLUX_GAME_LOG_KEEP}"
  [ "${terminating}" = "1" ] && return 2

  flux_banner "Counter-Strike 2"
  flux_log "starting the server (generation ${generation}, addons $([ "${addons_loaded}" = 1 ] && echo loaded || echo not loaded))"
  (
    cd "${s}" || exit 1
    export TERM=xterm
    # The server's own exit code goes to a file: `script` does not end when the server does (it
    # goes on reading the FIFO, which never closes), so the supervisor watches the server.
    exec script -qfec "${launch}; echo \$? >${FLUX_EXIT_FILE}" /dev/null <"${FLUX_CONSOLE_FIFO}"
  ) > >(console_writer) 2>&1 &
  game_pid=$!
  started_at="$(date -u +%s)"
  cs2 state starting "Loading ${CS2_MAP:-de_dust2}" >/dev/null 2>&1
  ready_watcher &
  ready_pid=$!
  # The hub waits for this to change to know a restart is over (never the clocks).
  printf '%s %s\n' "${generation}" "${started_at}" >"${FLUX_GENERATION_FILE}"
  backupper &
  backup_pid=$!
  return 0
}

sweep_generation() {
  [ -n "${ready_pid}" ] && kill "${ready_pid}" 2>/dev/null
  ready_pid=""
  [ -n "${backup_pid}" ] && kill "${backup_pid}" 2>/dev/null
  backup_pid=""
  pkill -KILL -f "${FLUX_GAME_PROCESS}" 2>/dev/null
  game_pid=""
}

# Waits for the server to end. `script` outlives it (see start_generation), so the server is what
# is watched: once it has run and is gone, `script` is ended too. A requested stop or restart that
# is not honoured within FLUX_STOP_GRACE ends the server by force; the grace runs from when the
# request was first seen. Sets game_rc to the server's exit code (143 when it had to be killed).
wait_for_generation() {
  local asked_at=0 now seen=0 killed=0
  while kill -0 "${game_pid}" 2>/dev/null; do
    sleep 1 &
    wait $!
    if pgrep -f "${FLUX_GAME_PROCESS}" >/dev/null 2>&1; then
      seen=1
    elif [ "${seen}" = "1" ] || [ -f "${FLUX_EXIT_FILE}" ]; then
      # KILL, not TERM: with the server gone `script` has nothing left to pass on, and it ignores
      # a TERM while it spins on the FIFO.
      kill -KILL "${game_pid}" 2>/dev/null
      break
    fi
    if [ -f "${FLUX_QUITTING_FILE}" ]; then
      now="$(date -u +%s)"
      if [ $((now - $(cat "${FLUX_QUITTING_FILE}" 2>/dev/null || echo "${now}"))) -ge "${FLUX_QUIT_GRACE}" ]; then
        pkill -KILL -f "${FLUX_GAME_PROCESS}" 2>/dev/null
        kill -KILL "${game_pid}" 2>/dev/null
        break
      fi
    fi
    if [ "${terminating}" = "1" ] || [ -f "${FLUX_RESTART_MARKER}" ]; then
      now="$(date -u +%s)"
      [ "${asked_at}" = "0" ] && asked_at="${now}"
      if [ $((now - asked_at)) -ge "${FLUX_STOP_GRACE}" ]; then
        flux_log "WARN the server was asked to stop ${FLUX_STOP_GRACE}s ago and has not; ending it"
        pkill -KILL -f "${FLUX_GAME_PROCESS}" 2>/dev/null
        kill -KILL "${game_pid}" 2>/dev/null
        killed=1
        break
      fi
    else
      asked_at=0
    fi
  done
  wait "${game_pid}" 2>/dev/null
  if [ "${killed}" = "1" ]; then
    game_rc=143
  elif [ -f "${FLUX_QUITTING_FILE}" ]; then
    # It had quit (its "Source2Shutdown" was seen): a quit, whatever the process returned after
    # (cs2 exits 127 after a clean quit, measured; with CounterStrikeSharp it never exits by itself).
    game_rc=0
  else
    game_rc="$(tr -dc '0-9' <"${FLUX_EXIT_FILE}" 2>/dev/null)"
    [ -n "${game_rc}" ] || game_rc=1
  fi
}

# The environment holds a value restarting cannot fix: the state says what to change, and the
# container waits (no restart budget used) for a stop or the panel's restart.
wait_for_fix() {
  flux_log "not starting: waiting for a redeploy with a usable GAME_PORT"
  while [ "${terminating}" = "0" ] && [ ! -f "${FLUX_RESTART_MARKER}" ]; do
    sleep 2 &
    wait $!
  done
  rm -f "${FLUX_RESTART_MARKER}"
}

while true; do
  if [ "${terminating}" = "1" ]; then
    flux_log "stopped on request before the server started"
    cs2 state stopped >/dev/null 2>&1
    exit 0
  fi
  start_generation
  started=$?
  if [ "${started}" = "3" ]; then
    wait_for_fix
    continue
  fi
  if [ "${started}" = "2" ]; then
    flux_log "stopped on request before the server started"
    cs2 state stopped >/dev/null 2>&1
    exit 0
  elif [ "${started}" != "0" ]; then
    flux_log "ERROR the server could not be prepared; ending the container (42)"
    cs2 state crashed "Counter-Strike 2 could not be installed or updated from Steam. Check the node's disk space and connection." >/dev/null 2>&1
    exit 42
  fi
  wait_for_generation
  rc="${game_rc}"
  ran=$(( $(date -u +%s) - started_at ))

  reason=""
  [ -f "${FLUX_RESTART_MARKER}" ] && reason="$(cat "${FLUX_RESTART_MARKER}" 2>/dev/null)"
  sweep_generation
  rm -f "${FLUX_RESTART_MARKER}"

  if [ "${terminating}" = "1" ]; then
    flux_log "stopped on request (server exited ${rc})"
    cs2 state stopped >/dev/null 2>&1
    exit 0
  fi

  # The server ends with 0 when it was told to quit (the console's quit, an admin's RCON quit):
  # that is a restart someone asked for.
  asked=0
  if [ -n "${reason}" ]; then
    asked=1
  elif [ "${rc}" = "0" ]; then
    reason="requested: the server quit (exit 0)"
    asked=1
  fi
  [ -z "${reason}" ] && reason="the server ended on its own (exit ${rc})"

  if flux_is_early_addon_crash "${ran}" "${rc}" "${addons_loaded}" "${asked}" "${FLUX_EARLY_CRASH_SECONDS}"; then
    early_crashes=$((early_crashes + 1))
    if [ "${early_crashes}" -ge 2 ]; then
      python3 -c 'import json,sys,time; json.dump({"reason": sys.argv[2], "since": int(time.time()), "build": sys.argv[3] or None}, open(sys.argv[1], "w"))' \
        "${safe_mode}" "the server crashed ${early_crashes} times in a row within ${FLUX_EARLY_CRASH_SECONDS}s of starting with the addons (exit ${rc})" "${build:-}"
      flux_log "addons: ${early_crashes} early crashes in a row with Metamod and CounterStrikeSharp: entering SAFE MODE (the next starts go without them)"
      early_crashes=0
    fi
  else
    early_crashes=0
  fi

  if flux_restart_counts "${reason}"; then
    now="$(date -u +%s)"
    read -r -a restart_history <<<"$(flux_restarts_in_window "${now}" "${FLUX_RESTART_WINDOW}" "${restart_history[@]}")"
    restart_history+=("${now}")
    if [ "${#restart_history[@]}" -gt "${FLUX_RESTART_MAX_ATTEMPTS}" ]; then
      flux_log "${reason}: ${#restart_history[@]} restarts within ${FLUX_RESTART_WINDOW}s, so restarting it here is not working. Ending the container so the platform can rebuild or move it (42)."
      cs2 state crashed "The server keeps stopping: ${reason}" >/dev/null 2>&1
      exit 42
    fi
  fi

  cs2 state restarting "${reason}" >/dev/null 2>&1
  flux_log "${reason}: restarting the server in place in ${FLUX_RESTART_BACKOFF}s"
  sleep "${FLUX_RESTART_BACKOFF}" &
  wait $!
done
