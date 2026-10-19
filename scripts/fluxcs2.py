#!/usr/bin/env python3
"""The hub's commands for the Counter-Strike 2 server's files. Each prints one JSON document.

    flux-cs2 files                     the game's maps, the Workshop maps downloaded, the map cycle, the addons
                                       (Metamod + CounterStrikeSharp: installed, versions, safe mode), the plugins,
                                       the plugin catalogue, the admins and the ban lists
    flux-cs2 set-mapcycle '<json>'     ["de_dust2", "de_mirage", ...] -> cfg/flux/mapcycle.txt, the map group mg_flux
                                       the server is started with; [] goes back to the game's mg_active
    flux-cs2 plugin-install <zip>      a CounterStrikeSharp plugin's .zip (as its author releases it), or a plugin
                                       folder's .dll: unpacked into css/plugins, its configs into css/configs and
                                       cfg/
    flux-cs2 plugin-add <id>           a plugin from the catalogue (plugins.json): its latest GitHub release, the same way
    flux-cs2 plugin-enable <name> on|off   off = moved to plugins/disabled (CounterStrikeSharp never loads it)
    flux-cs2 plugin-remove <name>      delete a plugin (its configs are kept)
    flux-cs2 set-admins '<json>'       [{"id": "<SteamID64 | STEAM_0:x:y | [U:1:n]>", "flags": ["@css/root"], "immunity": 100,
                                       "name": "..."}] -> css/configs/admins.json, MERGED: an admin's groups and
                                       command overrides are kept; admins left out are removed
    flux-cs2 addons-retry              leave safe mode: the next start loads Metamod and CounterStrikeSharp again
    flux-cs2 status                    ONE document: state (updating, starting, running, restarting, stopped, needs-fix,
                                       crashed), message, map, players (A2S), build, Steam token state, addons
    flux-cs2 players                   the roster: user id, name, bot, SteamID, ping, state, ip (from `status` + the log)
    flux-cs2 kick <userid> [reason]    flux-cs2 ban <steamid|ip> [minutes]   flux-cs2 unban <steamid|ip>
                                       bans are written to the data volume at once (writeid/writeip)
    flux-cs2 change-map <map|workshop id|link>
    flux-cs2 settings                  the Server Settings form: definitions and the values set
    flux-cs2 set-settings '<json>'     {"mp_roundtime": "2", "bot_quota": null}: validated, written and applied at once
    flux-cs2 backups                   the backups in flux/backups: name, size, date, why it was made
    flux-cs2 restore <backup>          restore one: the server quits, a safety backup of what is there now is
                                       taken, the backup replaces cfg/, css/ and the ban lists, and the server starts
                                       again (the supervisor does it)

For the supervisor (not the hub):
    fluxcs2.py backup <why> [force]    a backup of the data volume, unless one is younger than
                                       FLUX_BACKUP_MIN_AGE_MINUTES (60) and `force` is not given; then pruning
    fluxcs2.py apply-restore           a restore the hub asked for, before the server starts
    fluxcs2.py write-mapgroup          game/csgo/gamemodes_server.txt from cfg/flux/mapcycle.txt

Everything the owner makes is on the data volume (/mnt/cs2/data); the install links to it. Names are
file names, never paths: anything that would leave its folder is refused.
"""

import calendar
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile

DATA = os.environ.get("FLUX_DATA_DIR", "/mnt/cs2/data")
SERVER = os.environ.get("FLUX_SERVER_DIR", "/mnt/cs2/server")
GAME = os.path.join(SERVER, "game", "csgo")
CFG = os.path.join(DATA, "cfg")
MAPCYCLE = os.path.join(CFG, "flux", "mapcycle.txt")
GGMC_MAPS = os.path.join(CFG, "GGMCmaps.json")
GAMEMODES_SERVER = os.path.join(GAME, "gamemodes_server.txt")
CSS = os.path.join(DATA, "css")
PLUGINS = os.path.join(CSS, "plugins")
DISABLED = os.path.join(PLUGINS, "disabled")
CONFIGS = os.path.join(CSS, "configs")
SHARED = os.path.join(CSS, "shared")
# Plugins' own gamedata files: copied into the framework's gamedata/ (on the install) on every start.
GAMEDATA = os.path.join(CSS, "gamedata")
ADMINS = os.path.join(CONFIGS, "admins.json")
ADDONS = os.path.join(GAME, "addons")
STATE_DIR = os.path.join(DATA, "flux")
ADDON_STATE = os.path.join(STATE_DIR, "addons.json")
SAFE_MODE = os.path.join(STATE_DIR, "safe-mode")
PLUGIN_SOURCES = os.path.join(STATE_DIR, "plugin-sources.json")
BACKUPS = os.path.join(STATE_DIR, "backups")
BACKUP_REASONS = os.path.join(BACKUPS, "reasons.json")
RESTORE_REQUEST = os.path.join(STATE_DIR, "restore-request")
RESTART_MARKER = os.environ.get("FLUX_RESTART_MARKER", "/tmp/flux-restart-requested")
GENERATION_FILE = os.environ.get("FLUX_GENERATION_FILE", "/tmp/flux-generation")
CATALOG = os.environ.get("FLUX_PLUGIN_CATALOG", os.path.join(os.path.dirname(os.path.realpath(__file__)), "plugins.json"))
BANNED_USER = os.path.join(DATA, "banned_user.cfg")
BANNED_IP = os.path.join(DATA, "banned_ip.cfg")
# What a backup holds: everything the owner made, all on the data volume. Plugin logs are left out
# (they only grow); the game and the addons themselves are downloaded again on any node.
BACKUP_ITEMS = ["cfg", "css", "banned_user.cfg", "banned_ip.cfg"]
BACKUP_EXCLUDE = re.compile(r"^css/(logs|plugins/[^/]+/logs)(/|$)")
BACKUP_NAME = re.compile(r"^\d{8}-\d{6}-cs2\.tar\.gz$")

# The maps the game ships that are not maps to play: the main menu's, the lobby's, the benchmark.
NOT_PLAYABLE = re.compile(r"^(lobby_mapveto|graphics_settings|warmup|.*_vanity|.*_preview|.*_scene)$")
PLAYABLE_PREFIX = re.compile(r"^(de|cs|ar|dm|gd|dz)_")
MAP_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
PLUGIN_NAME = re.compile(r"^[A-Za-z0-9_.+-]{1,80}$")
# The most a plugin archive may unpack to (the hub's upload cap is 1 GB): a bomb stops here.
MAX_UNPACKED = 512 * 1024 * 1024


def out(data):
    print(json.dumps(data, ensure_ascii=False))


def fail(message, code=2):
    out({"ok": False, "error": message})
    sys.exit(code)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".flux-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def read_json(path, default):
    text = read(path)
    if text is None:
        return default
    try:
        # CounterStrikeSharp's own configs carry // comments; json cannot read them.
        return json.loads(re.sub(r"(?m)^\s*//.*$", "", text))
    except ValueError:
        return default


def generation():
    """(number, epoch) of the server the supervisor runs now: a restart is over when it changes."""
    parts = (read(GENERATION_FILE) or "").split()
    return {"n": int(parts[0]), "since": int(parts[1])} if len(parts) == 2 and all(p.isdigit() for p in parts) else None


# ------------------------------------------------------------------------------------------------
# Maps
# ------------------------------------------------------------------------------------------------

def stock_maps(maps_dir=None):
    """The playable maps the game ships: game/csgo/maps/<name>.vpk, without the menu's and lobby's."""
    folder = maps_dir or os.path.join(GAME, "maps")
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    found = set()
    for n in names:
        if not n.endswith(".vpk"):
            continue
        base = n[:-4]
        # A map's sound and lighting packs are <map>_<suffix>.vpk next to it, and its own is <map>.vpk.
        if PLAYABLE_PREFIX.match(base) and not NOT_PLAYABLE.match(base) and not re.search(r"_(dir|\d{3})$", base):
            found.add(base)
    return sorted(found)


def workshop_maps():
    """The Workshop items the server has downloaded (game/bin/linuxsteamrt64/steamapps/workshop/
    content/730/<id>): the ID, and the title and map name from the item's publish_data.txt
    ("title" "Aim Flow", "source_folder" "aim_flow", measured)."""
    base = os.path.join(SERVER, "game", "bin", "linuxsteamrt64", "steamapps", "workshop", "content", "730")
    rows = []
    try:
        ids = sorted(os.listdir(base))
    except OSError:
        return rows
    for i in ids:
        if not i.isdigit():
            continue
        meta = read(os.path.join(base, i, "publish_data.txt")) or ""
        title = re.search(r'"title"\s+"([^"]*)"', meta)
        folder = re.search(r'"source_folder"\s+"([^"]*)"', meta)
        size = 0
        try:
            size = sum(os.path.getsize(os.path.join(base, i, n)) for n in os.listdir(os.path.join(base, i)))
        except OSError:
            pass
        rows.append({"id": i, "title": title.group(1) if title else None, "map": folder.group(1) if folder else None, "size": size})
    return rows


def mapcycle():
    text = read(MAPCYCLE)
    return [l.strip() for l in (text or "").splitlines() if l.strip() and not l.strip().startswith("//")]


def render_gamemodes_server(maps):
    """gamemodes_server.txt with the one map group the panel owns, mg_flux."""
    rows = "".join(f'\t\t\t\t"{m}"\t""\n' for m in maps)
    return (
        "// Written by the Flux panel (runonflux/cs2-server-flux) from cfg/flux/mapcycle.txt. Edit the cycle in\n"
        "// the panel's Maps tab: this file is written again on every start.\n"
        '"GameModes_Server.txt"\n{\n\t"mapgroups"\n\t{\n\t\t"mg_flux"\n\t\t{\n\t\t\t"name"\t"mg_flux"\n\t\t\t"maps"\n\t\t\t{\n'
        f"{rows}\t\t\t}}\n\t\t}}\n\t}}\n}}\n"
    )


def cmd_write_mapgroup():
    maps = mapcycle()
    if maps:
        write_text(GAMEMODES_SERVER, render_gamemodes_server(maps))
    else:
        try:
            os.remove(GAMEMODES_SERVER)
        except OSError:
            pass
    out({"ok": True, "mapgroup": "mg_flux" if maps else "mg_active", "maps": maps})


def cmd_set_mapcycle(raw):
    try:
        maps = json.loads(raw)
    except ValueError:
        fail("not JSON")
    if not isinstance(maps, list) or not all(isinstance(m, str) and MAP_NAME.match(m) for m in maps):
        fail("a list of map names")
    seen = []
    for m in maps:
        if m not in seen:
            seen.append(m)
    if seen:
        write_text(MAPCYCLE, "".join(f"{m}\n" for m in seen))
    else:
        try:
            os.remove(MAPCYCLE)
        except OSError:
            pass
    if os.path.isdir(GAME):
        cmd_write_mapgroup_quiet()
    # GG1 MapChooser (the catalogue's map vote) reads its own list: the cycle, when it is installed.
    votes = False
    if seen and os.path.isfile(GGMC_MAPS):
        write_text(GGMC_MAPS, json.dumps({m: {"ws": False, "display": m, "mapid": "", "minplayers": 0, "maxplayers": 0, "weight": 1} for m in seen}, indent=2) + "\n")
        votes = True
    out({"ok": True, "mapcycle": seen, "mapgroup": "mg_flux" if seen else "mg_active", "mapVotes": votes})


def cmd_write_mapgroup_quiet():
    maps = mapcycle()
    if maps:
        write_text(GAMEMODES_SERVER, render_gamemodes_server(maps))
    elif os.path.exists(GAMEMODES_SERVER):
        os.remove(GAMEMODES_SERVER)


# ------------------------------------------------------------------------------------------------
# Plugins (CounterStrikeSharp): one folder per plugin, plugins/<Name>/<Name>.dll
# ------------------------------------------------------------------------------------------------

def catalog():
    try:
        with open(CATALOG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def plugin_dirs(folder):
    rows = []
    try:
        names = sorted(os.listdir(folder), key=str.lower)
    except OSError:
        return rows
    for n in names:
        p = os.path.join(folder, n)
        if n.lower() == "disabled" or not os.path.isdir(p):
            continue
        size = 0
        for root, _, fs in os.walk(p):
            for f in fs:
                try:
                    size += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        rows.append({"name": n, "dll": os.path.isfile(os.path.join(p, n + ".dll")), "size": size,
                     "modified": int(os.path.getmtime(p))})
    return rows


def plugins():
    sources = read_json(PLUGIN_SOURCES, {})
    rows = [dict(p, enabled=True) for p in plugin_dirs(PLUGINS)] + [dict(p, enabled=False) for p in plugin_dirs(DISABLED)]
    for r in rows:
        src = sources.get(r["name"]) or {}
        r["catalogId"] = src.get("id")
        r["version"] = src.get("version")
    return rows


def classify(path):
    """Where one file of a plugin archive belongs, as (area, relative path) or None to skip it.

    Authors pack their releases in every shape: addons/counterstrikesharp/plugins/X/..., counterstrikesharp/
    plugins/X/..., plugins/X/..., X/X.dll at the top, with cfg/... for the configs the plugin execs. Each
    path is read from the end that matters: everything up to a known folder is dropped."""
    parts = [p for p in path.replace("\\", "/").split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        return None
    lower = [p.lower() for p in parts]
    for i, p in enumerate(lower):
        if p == "counterstrikesharp" and i + 1 < len(parts):
            rest = lower[i + 1]
            if rest in ("plugins", "configs", "shared") and i + 2 < len(parts):
                return rest, "/".join(parts[i + 2:])
            # A plugin's own signatures (MatchZy ships gamedata/matchzy.json); never the framework's.
            if rest == "gamedata" and i + 2 == len(parts) - 1 and lower[-1] != "gamedata.json" and lower[-1].endswith(".json"):
                return "gamedata", parts[-1]
            return None  # the framework itself (bin/, api/, dotnet/): never from a plugin
        if p == "cfg" and i + 1 < len(parts) and "counterstrikesharp" not in lower[:i]:
            return "cfg", "/".join(parts[i + 1:])
        if p in ("plugins", "configs", "shared") and i + 1 < len(parts) and "addons" not in lower[i + 1:]:
            if p == "configs" and i + 1 < len(parts) and lower[i + 1] == "plugins":
                return "configs", "/".join(parts[i + 1:])
            return p, "/".join(parts[i + 1:])
        if p == "addons":
            # addons/<something else>: a Metamod plugin, not a CounterStrikeSharp one.
            if i + 1 < len(parts) and lower[i + 1] != "counterstrikesharp":
                return None
    return "loose", "/".join(parts)


def plan_archive(names):
    """{area: [(member, relative path)]} for the members of an archive, the loose ones placed under the
    folder their .dll names. Raises ValueError when it holds no plugin."""
    plan = {"plugins": [], "configs": [], "shared": [], "cfg": [], "gamedata": []}
    loose = []
    for n in names:
        if n.endswith("/"):
            continue
        c = classify(n)
        if not c:
            continue
        area, rel = c
        if area == "loose":
            loose.append((n, rel))
        else:
            plan[area].append((n, rel))
    if loose:
        # X/X.dll at the top (or X.dll alone): the plugin's folder is X.
        dlls = [rel for _, rel in loose if rel.lower().endswith(".dll")]
        named = [d for d in dlls if "/" in d and d.split("/")[-2].lower() == os.path.basename(d)[:-4].lower()]
        if named:
            top = named[0].split("/")[:-2]
            prefix = "/".join(top) + "/" if top else ""
            for n, rel in loose:
                if rel.startswith(prefix):
                    plan["plugins"].append((n, rel[len(prefix):]))
        elif len(dlls) >= 1 and not plan["plugins"]:
            # A bare folder of files with one main .dll: the folder is named after the archive's main .dll.
            main = sorted(dlls, key=lambda d: d.count("/"))[0]
            name = os.path.basename(main)[:-4]
            base = os.path.dirname(main)
            for n, rel in loose:
                if not base or rel.startswith(base + "/"):
                    plan["plugins"].append((n, name + "/" + (rel[len(base) + 1:] if base else rel)))
    # A plugin is a folder holding <folder>.dll.
    tops = {rel.split("/")[0] for _, rel in plan["plugins"] if "/" in rel}
    found = sorted(t for t in tops if any(rel.lower() == f"{t}/{t}.dll".lower() or rel.lower().endswith(f"/{t}.dll".lower()) for _, rel in plan["plugins"] if rel.startswith(t + "/")))
    if not found and not plan["shared"]:
        raise ValueError("no CounterStrikeSharp plugin in it: a plugin is a folder holding a .dll of the same name")
    for area in plan:
        for _, rel in plan[area]:
            if rel.startswith("/") or ".." in rel.split("/"):
                raise ValueError(f"a path that leaves its folder: {rel}")
    return plan, found


def install_archive(path):
    """Unpacks a plugin zip into the data volume. Returns the plugin names it installed."""
    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile):
        raise ValueError("not a .zip file")
    with z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > MAX_UNPACKED:
            raise ValueError("the archive unpacks to more than 512 MB")
        plan, names = plan_archive([i.filename for i in infos])
        targets = {"plugins": PLUGINS, "configs": CONFIGS, "shared": SHARED, "cfg": CFG, "gamedata": GAMEDATA}
        for name in names:
            # A plugin switched off stays off when it is updated.
            off = os.path.isdir(os.path.join(DISABLED, name)) and not os.path.isdir(os.path.join(PLUGINS, name))
            if off:
                targets_plugins = DISABLED
            else:
                targets_plugins = PLUGINS
            for member, rel in plan["plugins"]:
                if rel.split("/")[0] == name:
                    dst = os.path.join(targets_plugins, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with z.open(member) as src, open(dst + ".flux-new", "wb") as o:
                        shutil.copyfileobj(src, o)
                    os.replace(dst + ".flux-new", dst)
        for area in ("configs", "shared", "cfg", "gamedata"):
            for member, rel in plan[area]:
                dst = os.path.join(targets[area], rel)
                # Configs the owner already has are theirs: an update never overwrites one.
                if area in ("configs", "cfg") and os.path.exists(dst):
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with z.open(member) as src, open(dst + ".flux-new", "wb") as o:
                    shutil.copyfileobj(src, o)
                os.replace(dst + ".flux-new", dst)
    sync_gamedata()
    link_cfg_entries()
    # CounterStrikeSharp loads shared libraries only as it starts (PluginManager.LoadSharedLibraries):
    # a plugin that brought one loads after a restart, not with `css_plugins load` (measured,
    # GG1MapChooser: "Could not load file or assembly 'ggmcAPI'").
    return names, bool(plan["shared"])


def link_cfg_entries():
    """A plugin's new cfg folder (MatchZy's cfg/MatchZy) linked into the install's csgo/cfg at once,
    as the supervisor does on every start, so the plugin finds it without a restart."""
    target = os.path.join(GAME, "cfg")
    if not os.path.isdir(target) or not os.path.isdir(CFG):
        return
    for n in os.listdir(CFG):
        dst = os.path.join(target, n)
        if not os.path.lexists(dst):
            os.symlink(os.path.join(CFG, n), dst)


def sync_gamedata():
    """Copies the plugins' gamedata files into the framework's gamedata folder (the install's)."""
    dst = os.path.join(ADDONS, "counterstrikesharp", "gamedata")
    if not os.path.isdir(GAMEDATA) or not os.path.isdir(dst):
        return
    for n in os.listdir(GAMEDATA):
        if n.endswith(".json") and n != "gamedata.json":
            shutil.copyfile(os.path.join(GAMEDATA, n), os.path.join(dst, n))


def remember_source(names, entry=None, version=None):
    sources = read_json(PLUGIN_SOURCES, {})
    for n in names:
        sources[n] = {"id": entry["id"] if entry else None, "version": version, "installed": int(time.time())}
    write_text(PLUGIN_SOURCES, json.dumps(sources, indent=1))


def cmd_plugin_install(src):
    if not os.path.isfile(src):
        fail("the uploaded file is not there")
    try:
        names, restart = install_archive(src)
    except ValueError as e:
        fail(str(e))
    remember_source(names)
    out({"ok": True, "plugins": names, "restartNeeded": restart})


def github_latest(repo, asset_re):
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "runonflux-cs2-server-flux"})
    with urllib.request.urlopen(req, timeout=30) as r:
        rel = json.load(r)
    for a in rel.get("assets", []):
        if re.search(asset_re, a.get("name", "")):
            return rel.get("tag_name"), a["browser_download_url"], a["name"]
    raise ValueError(f"the latest release of {repo} has no file matching {asset_re}")


def cmd_plugin_add(plugin_id):
    entry = next((p for p in catalog() if p["id"] == plugin_id), None)
    if not entry:
        fail(f"not in the catalogue: {plugin_id}")
    try:
        tag, url, name = github_latest(entry["repo"], entry["asset"])
        tmp = tempfile.mkdtemp(prefix="flux-plugin-")
        path = os.path.join(tmp, os.path.basename(name))
        req = urllib.request.Request(url, headers={"User-Agent": "runonflux-cs2-server-flux"})
        with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as o:
            shutil.copyfileobj(r, o)
        try:
            names, restart = install_archive(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except ValueError as e:
        fail(str(e))
    except OSError as e:
        fail(f"could not download {entry['name']} from GitHub: {e}")
    remember_source(names, entry, tag)
    out({"ok": True, "plugins": names, "version": tag, "id": plugin_id, "restartNeeded": restart})


def cmd_plugin_enable(name, on):
    if not PLUGIN_NAME.match(name or "") or name.lower() == "disabled":
        fail(f"not an allowed plugin name: {name or '(empty)'}")
    on = str(on).lower() in ("1", "on", "true", "yes")
    src, dst = (os.path.join(DISABLED, name), os.path.join(PLUGINS, name)) if on else (os.path.join(PLUGINS, name), os.path.join(DISABLED, name))
    if not os.path.isdir(src):
        if os.path.isdir(dst):
            out({"ok": True, "plugin": name, "enabled": on})
            return
        fail("no such plugin")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        shutil.rmtree(dst)
    os.replace(src, dst)
    out({"ok": True, "plugin": name, "enabled": on})


def cmd_plugin_remove(name):
    if not PLUGIN_NAME.match(name or "") or name.lower() == "disabled":
        fail(f"not an allowed plugin name: {name or '(empty)'}")
    removed = False
    for folder in (PLUGINS, DISABLED):
        p = os.path.join(folder, name)
        if os.path.isdir(p):
            shutil.rmtree(p)
            removed = True
    if not removed:
        fail("no such plugin")
    sources = read_json(PLUGIN_SOURCES, {})
    if sources.pop(name, None) is not None:
        write_text(PLUGIN_SOURCES, json.dumps(sources, indent=1))
    out({"ok": True, "plugin": name})


# ------------------------------------------------------------------------------------------------
# Admins: CounterStrikeSharp's configs/admins.json
# ------------------------------------------------------------------------------------------------

STEAM64_BASE = 76561197960265728
CSS_FLAG = re.compile(r"^@[a-z0-9_-]{1,32}/[a-z0-9_-]{1,32}$")


def steam64(value):
    """The SteamID64 of a SteamID64, a [U:1:n] or a STEAM_x:y:z; None otherwise."""
    v = str(value or "").strip()
    m = re.fullmatch(r"STEAM_[0-5]:([01]):(\d{1,10})", v)
    if m:
        return str(STEAM64_BASE + int(m.group(2)) * 2 + int(m.group(1)))
    m = re.fullmatch(r"\[?U:1:(\d{1,10})\]?", v)
    if m:
        return str(STEAM64_BASE + int(m.group(1)))
    if re.fullmatch(r"7656119\d{10}", v):
        return v
    return None


def parse_admins(data):
    rows = []
    for name, a in (data or {}).items() if isinstance(data, dict) else []:
        if not isinstance(a, dict):
            continue
        rows.append({"name": name, "id": str(a.get("identity", "")), "steam64": steam64(a.get("identity")),
                     "flags": [f for f in a.get("flags", []) if isinstance(f, str)], "groups": a.get("groups") or [],
                     "immunity": int(a.get("immunity") or 0) if str(a.get("immunity") or 0).isdigit() else 0,
                     "editable": bool(steam64(a.get("identity")))})
    return rows


def cmd_set_admins(raw):
    try:
        admins = json.loads(raw)
    except ValueError:
        fail("not JSON")
    if not isinstance(admins, list):
        fail("a list of admins")
    existing = read_json(ADMINS, {})
    if not isinstance(existing, dict):
        existing = {}
    by_id = {steam64(a.get("identity")): (k, a) for k, a in existing.items() if isinstance(a, dict) and steam64(a.get("identity"))}
    result = {k: a for k, a in existing.items() if not (isinstance(a, dict) and steam64(a.get("identity")))}
    for a in admins:
        if not isinstance(a, dict):
            fail("each admin is an object")
        sid = steam64(a.get("id"))
        if not sid:
            fail(f"not a SteamID: {a.get('id')}")
        flags = a.get("flags") or []
        if not isinstance(flags, list) or not all(isinstance(f, str) and CSS_FLAG.match(f) for f in flags):
            fail("flags are CounterStrikeSharp permissions like @css/kick")
        try:
            immunity = int(a.get("immunity") or 0)
        except (TypeError, ValueError):
            fail("immunity is a number")
        if not 0 <= immunity <= 100:
            fail("immunity is 0 to 100")
        name = re.sub(r"[\r\n\"\\]", "", str(a.get("name") or "")).strip()[:60] or sid
        old_key, old = by_id.get(sid, (None, {}))
        entry = dict(old)
        entry.update({"identity": sid, "immunity": immunity, "flags": sorted(set(flags))})
        key = name
        while key in result:
            key = f"{name} ({sid[-4:]})"
            if key in result:
                key = f"{name} {sid}"
                break
        result[key] = entry
    write_text(ADMINS, json.dumps(result, indent=2) + "\n")
    out({"ok": True, "admins": parse_admins(result)})


# ------------------------------------------------------------------------------------------------
# Bans: banned_user.cfg (banid <minutes> <SteamID>) and banned_ip.cfg (addip <minutes> <ip>)
# ------------------------------------------------------------------------------------------------

def parse_bans(user_text, ip_text):
    bans = []
    for line in (user_text or "").splitlines():
        m = re.match(r"^\s*banid\s+(\d+(?:\.\d+)?)\s+(\S+)", line)
        if m:
            bans.append({"kind": "id", "id": m.group(2), "steam64": steam64(m.group(2)), "minutes": float(m.group(1))})
    for line in (ip_text or "").splitlines():
        m = re.match(r"^\s*addip\s+(\d+(?:\.\d+)?)\s+(\S+)", line)
        if m:
            bans.append({"kind": "ip", "id": m.group(2), "minutes": float(m.group(1))})
    return bans


# ------------------------------------------------------------------------------------------------
# files: everything the tabs show, in one call
# ------------------------------------------------------------------------------------------------

def addons_state():
    state = read_json(ADDON_STATE, {})
    safe = read_json(SAFE_MODE, None) if os.path.exists(SAFE_MODE) else None
    return {
        "metamod": state.get("metamod"),
        "css": state.get("css"),
        "installed": os.path.isfile(os.path.join(ADDONS, "counterstrikesharp", "bin", "linuxsteamrt64", "counterstrikesharp.so")),
        "loaded": bool(state.get("loaded")),
        "safeMode": safe,
    }


def cmd_files():
    cat = catalog()
    installed = {p.get("catalogId") for p in plugins() if p.get("catalogId")}
    out({
        "generation": generation(),
        "stock": stock_maps(),
        "workshop": workshop_maps(),
        "mapcycle": mapcycle(),
        "addons": addons_state(),
        "plugins": plugins(),
        "catalog": [dict(c, installed=c["id"] in installed) for c in cat],
        "admins": parse_admins(read_json(ADMINS, {})),
        "bans": parse_bans(read(BANNED_USER), read(BANNED_IP)),
    })


def cmd_addons_retry():
    try:
        os.remove(SAFE_MODE)
        cleared = True
    except OSError:
        cleared = False
    out({"ok": True, "cleared": cleared})


# ------------------------------------------------------------------------------------------------
# Backups and restores, as runonflux/tf2-server-flux: a .tar.gz of the owner's files; the newest
# FLUX_BACKUP_KEEP, plus the newest one older than a day and the newest one older than a week.
# ------------------------------------------------------------------------------------------------

def backup_rows():
    reasons = read_json(BACKUP_REASONS, {})
    rows = []
    for n in os.listdir(BACKUPS) if os.path.isdir(BACKUPS) else []:
        if BACKUP_NAME.match(n):
            st = os.stat(os.path.join(BACKUPS, n))
            stamp = calendar.timegm(time.strptime(n[:15], "%Y%m%d-%H%M%S"))
            rows.append({"name": n, "size": st.st_size, "created": int(stamp), "reason": reasons.get(n)})
    return sorted(rows, key=lambda r: r["name"], reverse=True)


def backups_to_prune(rows, keep, now):
    """rows newest first. Keep the newest `keep`, plus the newest one older than a day and the
    newest one older than a week. Returns the names to delete."""
    kept = {r["name"] for r in rows[:max(keep, 1)]}
    for age in (86400, 7 * 86400):
        older = next((r for r in rows if now - r["created"] >= age), None)
        if older:
            kept.add(older["name"])
    return [r["name"] for r in rows if r["name"] not in kept]


def cmd_backups():
    pending = read(RESTORE_REQUEST)
    out({"backups": backup_rows(), "pendingRestore": pending.strip() if pending else None})


def make_backup(reason):
    os.makedirs(BACKUPS, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-cs2.tar.gz"
    path = os.path.join(BACKUPS, name)
    while os.path.exists(path):  # two in the same second (a restore's safety backup)
        time.sleep(1)
        name = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-cs2.tar.gz"
        path = os.path.join(BACKUPS, name)
    part = path + ".part"

    def skip(info):
        return None if BACKUP_EXCLUDE.match(info.name) else info

    with tarfile.open(part, "w:gz") as tar:
        for item in BACKUP_ITEMS:
            src = os.path.join(DATA, item)
            if os.path.lexists(src):
                tar.add(src, arcname=item, filter=skip)
    os.replace(part, path)
    reasons = read_json(BACKUP_REASONS, {})
    reasons[name] = reason
    names = {r["name"] for r in backup_rows()}
    write_text(BACKUP_REASONS, json.dumps({k: v for k, v in reasons.items() if k in names or k == name}))
    return name


def cmd_backup(reason, force=None):
    rows = backup_rows()
    min_age = int(os.environ.get("FLUX_BACKUP_MIN_AGE_MINUTES", "60") or 60) * 60
    now = time.time()
    if force != "force" and rows and now - rows[0]["created"] < min_age:
        out({"ok": True, "skipped": f"a backup from {int((now - rows[0]['created']) // 60)} min ago is recent enough"})
        return
    name = make_backup(reason)
    pruned = backups_to_prune(backup_rows(), int(os.environ.get("FLUX_BACKUP_KEEP", "10") or 10), now)
    for n in pruned:
        try:
            os.remove(os.path.join(BACKUPS, n))
        except OSError:
            pass
    out({"ok": True, "backup": name, "size": os.path.getsize(os.path.join(BACKUPS, name)), "pruned": pruned})


def cmd_restore(name):
    name = os.path.basename(str(name or "").strip())
    if not BACKUP_NAME.match(name):
        fail("not a backup name")
    if not os.path.isfile(os.path.join(BACKUPS, name)):
        fail("no such backup")
    write_text(RESTORE_REQUEST, name + "\n")
    # The supervisor restores between two servers: the running one quits (a requested restart),
    # and the next start applies the request before the server runs.
    write_text(RESTART_MARKER, f"requested: restore {name}\n")
    try:
        sent = subprocess.run(["flux-console", "quit"], capture_output=True, text=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        sent = False
    out({"ok": True, "restoring": name, "serverQuit": sent,
         "note": None if sent else "The server is not running: the backup is restored when it next starts."})


def cmd_apply_restore():
    name = (read(RESTORE_REQUEST) or "").strip()
    if not name:
        out({"ok": True, "restored": None})
        return
    try:
        os.remove(RESTORE_REQUEST)
    except OSError:
        pass
    src = os.path.join(BACKUPS, name)
    if not BACKUP_NAME.match(name) or not os.path.isfile(src):
        fail(f"the backup to restore is gone: {name}")
    safety = make_backup(f"before restoring {name}")
    staging = tempfile.mkdtemp(dir=STATE_DIR, prefix="restore-")
    try:
        with tarfile.open(src, "r:gz") as tar:
            tar.extractall(staging, filter="data")
        for item in BACKUP_ITEMS:
            new, cur = os.path.join(staging, item), os.path.join(DATA, item)
            if not os.path.lexists(new):
                continue
            old = cur + ".flux-old"
            if os.path.lexists(old):
                shutil.rmtree(old) if os.path.isdir(old) and not os.path.islink(old) else os.remove(old)
            if os.path.lexists(cur):
                os.replace(cur, old)
            os.replace(new, cur)
            if os.path.lexists(old):
                shutil.rmtree(old) if os.path.isdir(old) and not os.path.islink(old) else os.remove(old)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    out({"ok": True, "restored": name, "safetyBackup": safety})


# ------------------------------------------------------------------------------------------------
# State and status: what the hub shows first
# ------------------------------------------------------------------------------------------------
STATE_FILE = os.environ.get("FLUX_STATE_FILE", "/tmp/flux-state.json")
LOG_DIR = os.environ.get("FLUX_GAME_LOG_DIR", os.path.join(SERVER, "flux-logs"))
A2S_INFO = b"\xff\xff\xff\xffTSource Engine Query\x00"
A2S_PLAYER = b"\xff\xff\xff\xffU"


def game_port():
    try:
        return int(os.environ.get("GAME_PORT") or 27015)
    except ValueError:
        return 27015


def load_state():
    data = read_json(STATE_FILE, {})
    return data if isinstance(data, dict) else {}


def cmd_state(new, message=""):
    """The supervisor records what it is doing: updating, starting, running, restarting, stopped,
    needs-fix (the environment holds something restarting cannot fix), crashed."""
    state = load_state()
    gen = generation()
    state["state"] = new
    state["message"] = message
    state["since"] = int(time.time())
    if new == "running":
        state["runningSince"] = int(time.time())
    elif new in ("updating", "starting", "restarting", "stopped", "needs-fix", "crashed"):
        state["runningSince"] = None
    state["generation"] = gen["n"] if gen else state.get("generation", 0)
    write_text(STATE_FILE, json.dumps(state))


def _cstring(data, at):
    end = data.index(b"\x00", at)
    return data[at:end].decode("utf-8", "replace"), end + 1


def a2s(request, port=None, host="127.0.0.1", timeout=2.0):
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sent = request + b"\xff\xff\xff\xff" if request == A2S_PLAYER else request
        for _ in range(3):
            sock.sendto(sent, (host, port or game_port()))
            data, _ = sock.recvfrom(4096)
            if data[:4] != b"\xff\xff\xff\xff":
                raise ValueError("split or malformed reply")
            body = data[4:]
            if body[:1] == b"\x41" and len(body) >= 5:
                sent = request + body[1:5]
                continue
            return body
        raise ValueError("challenged too often")
    finally:
        sock.close()


def parse_info(body):
    """An A2S_INFO reply without its 4-byte header -> dict. ValueError otherwise."""
    if len(body) < 2 or body[0] != 0x49:
        raise ValueError("not an A2S_INFO reply")
    at = 2
    name, at = _cstring(body, at)
    game_map, at = _cstring(body, at)
    _folder, at = _cstring(body, at)
    game, at = _cstring(body, at)
    at += 2
    players, max_players, bots = body[at], body[at + 1], body[at + 2]
    at += 3
    _kind, _env, visibility, vac = body[at:at + 4]
    at += 4
    version, at = _cstring(body, at)
    return {"name": name, "map": game_map, "game": game, "players": players, "maxPlayers": max_players,
            "bots": bots, "password": bool(visibility), "vac": bool(vac), "version": version}


def parse_players(body):
    """An A2S_PLAYER reply without its header -> [{"name", "score", "seconds"}]."""
    import struct
    if len(body) < 2 or body[0] != 0x44:
        raise ValueError("not an A2S_PLAYER reply")
    at = 2
    players = []
    for _ in range(body[1]):
        at += 1
        name, at = _cstring(body, at)
        score, seconds = struct.unpack_from("<lf", body, at)
        at += 8
        if name:
            players.append({"name": name, "score": score, "seconds": int(seconds)})
    return players


def query():
    """(info, players) from the server on its game port, or (None, None) when it does not answer."""
    try:
        info = parse_info(a2s(A2S_INFO))
    except (OSError, ValueError, IndexError):
        return None, None
    try:
        players = parse_players(a2s(A2S_PLAYER))
    except (OSError, ValueError, IndexError):
        players = None
    return info, players


def cmd_probe():
    info, _ = query()
    if info is None:
        sys.exit(1)
    print(info["players"])


def gslt_value():
    return " ".join(os.environ.get("CS2_GSLT", "").split())


def gslt_state():
    value = gslt_value()
    if not value:
        return "missing"
    return "set" if re.fullmatch(r"[0-9A-Fa-f]{32}", value) else "invalid"


def cmd_gslt_state():
    print(gslt_state())


def console_tail(max_bytes=400_000):
    """The newest console log's last lines (text), or ''."""
    try:
        logs = sorted((os.path.join(LOG_DIR, n) for n in os.listdir(LOG_DIR) if n.endswith("-console.log")), key=os.path.getmtime)
    except OSError:
        return ""
    if not logs:
        return ""
    with open(logs[-1], "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - max_bytes))
        return f.read().decode("utf-8", "replace")


def steam_login(status_text):
    """What the server's own `status` says about its Steam login, measured (build 1.41.9.1):
    a valid token: steamid [G:1:..] "secure" with a public address; no token: an anonymous [A:1:..];
    a token Steam did not accept: steamid [I:0:1] and "disconnected from Steam3". Returns
    "ok" | "anonymous" | "failed" | None (unknown)."""
    m = re.search(r"^steamid\s*:\s*\[(\w):", status_text or "", re.M)
    if not m:
        return None
    if "disconnected from Steam3" in status_text or m.group(1) == "I":
        return "failed"
    return {"G": "ok", "A": "anonymous"}.get(m.group(1))


STEAM_CACHE = os.environ.get("FLUX_STEAM_CACHE", "/tmp/flux-steam-login.json")


def cached_steam_login(max_age=30):
    """steam_login() of the running server, asked at most every `max_age` seconds."""
    cached = read_json(STEAM_CACHE, {})
    if cached.get("at", 0) > time.time() - max_age and cached.get("gen") == (generation() or {}).get("n"):
        return cached.get("login")
    answer = console("status")
    login = steam_login(answer) if answer is not None else None
    try:
        write_text(STEAM_CACHE, json.dumps({"at": int(time.time()), "gen": (generation() or {}).get("n"), "login": login}))
    except OSError:
        pass
    return login


def installed_build():
    text = read(os.path.join(GAME, "steam.inf")) or ""
    m = re.search(r"^PatchVersion=([\d.]+)", text, re.M)
    return m.group(1) if m else None


def cmd_status():
    state = load_state()
    gen = generation()
    info, players = (None, None)
    if state.get("state") in ("running", "starting"):
        info, players = query()
    g = gslt_state()
    login = cached_steam_login() if state.get("state") == "running" else None
    if g == "missing":
        gm = "The server has no Steam login token (CS2_GSLT): it runs, but is not in the Internet server list. Make one at steamcommunity.com/dev/managegameservers with App ID 730."
    elif g == "invalid":
        gm = "CS2_GSLT is not a 32-character token (digits and letters A-F). It was left out: the server is not logged into Steam."
    elif g == "set" and login == "failed":
        gm = "Steam did not accept the login token (revoked, made for another game, or in use by another running server), or Steam could not be reached. Players cannot join. Make a new token for App ID 730 at steamcommunity.com/dev/managegameservers."
    else:
        gm = ""
    out({
        "state": state.get("state") or "unknown",
        "message": state.get("message") or "",
        "since": state.get("since"),
        "runningSince": state.get("runningSince"),
        "generation": gen["n"] if gen else int(state.get("generation") or 0),
        "gameMode": (os.environ.get("CS2_GAME_MODE") or "competitive").lower(),
        "serverName": info["name"] if info else (os.environ.get("CS2_HOSTNAME") or None),
        "map": info["map"] if info else None,
        "maxPlayers": info["maxPlayers"] if info else (int(os.environ["CS2_MAXPLAYERS"]) if os.environ.get("CS2_MAXPLAYERS", "").isdigit() else 10),
        "passworded": bool(os.environ.get("CS2_PASSWORD")),  # CS2's A2S reply does not flag sv_password
        "rcon": bool(os.environ.get("CS2_RCON_PASSWORD")),
        "port": game_port(),
        "build": installed_build(),
        "image": os.environ.get("FLUX_IMAGE_VERSION", "dev"),
        "query": info is not None,
        "playerCount": info["players"] if info else None,
        "botCount": info["bots"] if info else None,
        "players": players if players is not None else ([] if info else None),
        "gslt": {"state": g, "login": login, "message": gm},
        "addons": addons_state(),
        "restartPending": os.path.exists(RESTART_MARKER),
        "restorePending": os.path.exists(RESTORE_REQUEST),
        "lastBackup": (backup_rows() or [{}])[0].get("created"),
    })


# ------------------------------------------------------------------------------------------------
# Server Settings: cfg/flux/settings.cfg, run after each game mode's own rules
# ------------------------------------------------------------------------------------------------
SETTINGS_FILE = os.path.join(CFG, "flux", "settings.cfg")
CONSOLE = os.environ.get("FLUX_CONSOLE_CMD", "/opt/flux/flux-console.sh")

# key, label, group, type, default (the Competitive mode's), min, max / options, help
def _d(key, label, group, typ, default, lo=None, hi=None, options=None, help=""):
    return {"key": key, "label": label, "group": group, "type": typ, "default": default, "min": lo, "max": hi,
            "options": options, "help": help}


SETTING_DEFS = [
    _d("mp_roundtime", "Round time (minutes)", "Rounds", "float", 1.92, 0.5, 60, help="Per round. The mode's own value is used until you set one."),
    _d("mp_freezetime", "Freeze time (seconds)", "Rounds", "int", 15, 0, 60),
    _d("mp_maxrounds", "Rounds per match", "Rounds", "int", 24, 0, 100, help="0 = the mode's own."),
    _d("mp_overtime_enable", "Overtime on a tie", "Rounds", "bool", 1),
    _d("mp_halftime", "Switch sides at half time", "Rounds", "bool", 1),
    _d("mp_warmuptime", "Warmup (seconds)", "Rounds", "int", 30, 0, 600),
    _d("mp_do_warmup_period", "Warmup before the match", "Rounds", "bool", 1),
    _d("mp_c4timer", "Bomb timer (seconds)", "Rounds", "int", 40, 10, 90),
    _d("mp_buytime", "Buy time (seconds)", "Economy", "int", 20, 0, 600),
    _d("mp_startmoney", "Starting money", "Economy", "int", 800, 0, 16000),
    _d("mp_maxmoney", "Maximum money", "Economy", "int", 16000, 800, 100000),
    _d("mp_playercashawards", "Money for kills and objectives", "Economy", "bool", 1),
    _d("mp_teamcashawards", "Money for round results", "Economy", "bool", 1),
    _d("mp_friendlyfire", "Friendly fire", "Rules", "bool", 1, help="Teammates can hurt each other."),
    _d("mp_autoteambalance", "Balance the teams automatically", "Rules", "bool", 1),
    _d("mp_limitteams", "Team size difference allowed", "Rules", "int", 1, 0, 10, help="0 = no limit."),
    _d("mp_forcecamera", "Spectate teammates only when dead", "Rules", "select", 1, options=[{"v": 0, "l": "Anyone"}, {"v": 1, "l": "Own team"}, {"v": 2, "l": "Fixed camera"}]),
    _d("mp_respawn_on_death_ct", "Respawn Counter-Terrorists", "Rules", "bool", 0),
    _d("mp_respawn_on_death_t", "Respawn Terrorists", "Rules", "bool", 0),
    _d("sv_infinite_ammo", "Infinite ammo", "Rules", "select", 0, options=[{"v": 0, "l": "Off"}, {"v": 1, "l": "Infinite, no reload"}, {"v": 2, "l": "Infinite, reload"}]),
    _d("sv_gravity", "Gravity", "Rules", "int", 800, 100, 3000),
    _d("bot_quota", "Bots", "Bots", "int", 0, 0, 64, help="Bots fill empty places up to this number."),
    _d("bot_quota_mode", "Bot mode", "Bots", "select", "normal", options=[{"v": "normal", "l": "Always this many"}, {"v": "fill", "l": "Fill empty slots"}, {"v": "match", "l": "Match humans"}]),
    _d("bot_difficulty", "Bot difficulty", "Bots", "select", 1, options=[{"v": 0, "l": "Easy"}, {"v": 1, "l": "Normal"}, {"v": 2, "l": "Hard"}, {"v": 3, "l": "Expert"}]),
    _d("bot_join_after_player", "Bots wait for a human", "Bots", "bool", 1),
    _d("sv_alltalk", "Everyone hears everyone", "Voice and chat", "bool", 0),
    _d("sv_deadtalk", "Dead players talk to the living", "Voice and chat", "bool", 0),
    _d("sv_voiceenable", "Voice chat", "Voice and chat", "bool", 1),
    _d("sv_allow_votes", "Players can call votes", "Server", "bool", 1),
    _d("sv_cheats", "Cheats", "Server", "bool", 0, help="Lets players use cheat commands; no match is ranked."),
    _d("sv_pausable", "Players can pause", "Server", "bool", 0),
    _d("sv_hibernate_when_empty", "Sleep when empty", "Server", "bool", 1, help="Saves CPU while nobody is on; the first player wakes it in a second."),
    _d("sv_kick_players_with_cooldown", "Kick players with a Valve cooldown", "Server", "select", 0, options=[{"v": 0, "l": "No"}, {"v": 1, "l": "Kick"}, {"v": 2, "l": "Kick and block"}]),
    _d("sv_region", "Region (server list)", "Server", "select", 255, options=[
        {"v": 255, "l": "World"}, {"v": 0, "l": "US East"}, {"v": 1, "l": "US West"}, {"v": 2, "l": "South America"},
        {"v": 3, "l": "Europe"}, {"v": 4, "l": "Asia"}, {"v": 5, "l": "Australia"}, {"v": 6, "l": "Middle East"}, {"v": 7, "l": "Africa"}]),
]
SETTING_BY_KEY = {d["key"]: d for d in SETTING_DEFS}


def setting_value(d, raw):
    """(normalised value, None) or (None, why not)."""
    v = str(raw).strip().lower() if isinstance(raw, (str, bool, int, float)) else ""
    if v == "":
        return None, "empty"
    t = d["type"]
    if t == "bool":
        if v in ("1", "true", "yes", "on"):
            return "1", None
        if v in ("0", "false", "no", "off"):
            return "0", None
        return None, "must be on or off"
    if t == "select":
        for o in d["options"]:
            if str(o["v"]).lower() == v:
                return str(o["v"]), None
        return None, "not one of " + ", ".join(str(o["v"]) for o in d["options"])
    try:
        n = float(v) if t == "float" else int(v)
    except ValueError:
        return None, "must be a number"
    if (d["min"] is not None and n < d["min"]) or (d["max"] is not None and n > d["max"]):
        return None, f"must be {d['min']} to {d['max']}"
    return (repr(round(n, 2)) if t == "float" else str(n)), None


def current_settings():
    values = {}
    for line in (read(SETTINGS_FILE) or "").splitlines():
        m = re.match(r"^\s*([a-z0-9_]+)\s+(\S+)\s*(?://.*)?$", line)
        if m and m.group(1) in SETTING_BY_KEY:
            values[m.group(1)] = m.group(2).strip('"')
    return values


def cmd_settings():
    out({"defs": SETTING_DEFS, "values": current_settings(), "file": "cfg/flux/settings.cfg"})


def console(command):
    """One command typed into the running server; its answer, or None when no server runs."""
    try:
        env = dict(os.environ, FLUX_CONSOLE_UNTIL="#end" if command == "status" else "")
        r = subprocess.run([CONSOLE, command], capture_output=True, text=True, timeout=15, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def cmd_set_settings(raw):
    """{"mp_roundtime": "2", "bot_quota": null}: validated key by key, written at once (so a stop in the
    next second loses nothing), applied to the running server too. null or "" = back to the mode's own."""
    try:
        wanted = json.loads(raw)
    except ValueError:
        fail("Not valid JSON.")
    if not isinstance(wanted, dict):
        fail("Expected an object of settings.")
    values = current_settings()
    refused, reset = {}, []
    for key, raw_value in wanted.items():
        d = SETTING_BY_KEY.get(key)
        if d is None:
            refused[key] = "not a setting the panel manages"
            continue
        if raw_value is None or raw_value == "":
            if values.pop(key, None) is not None:
                reset.append(key)
            continue
        value, why = setting_value(d, raw_value)
        if why:
            refused[key] = why
        else:
            values[key] = value
    lines = ["// Written by the panel (Server Settings). Run after each game mode's own rules."]
    lines += [f"{k} {values[k]}" for k in (d["key"] for d in SETTING_DEFS) if k in values]
    write_text(SETTINGS_FILE, "\n".join(lines) + "\n")
    applied = []
    for key in (k for k in wanted if k in values and k not in refused):
        if console(f"{key} {values[key]}") is not None:
            applied.append(key)
    out({"ok": not refused, "saved": sorted(values), "reset": reset, "applied": applied, "refused": refused,
         "note": "Round and economy rules take effect when the next round or match starts."})


# ------------------------------------------------------------------------------------------------
# Players: the roster, kick, ban, change map
# ------------------------------------------------------------------------------------------------
ROSTER_ROW = re.compile(r"^\s*(\d+)\s+(\S+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\d+)\s*(\S*)\s+'(.*)'\s*$")
LOG_PLAYER = re.compile(r'"(.*)<(\d+)><(\[U:1:\d+\]|BOT|STEAM_ID_PENDING|[^<>]*)>(?:<[^<>]*>)?"\s+(connected|entered the game|disconnected)')


def steam_u(value):
    """[U:1:n] for a SteamID64, [U:1:n] or STEAM_x:y:z; None otherwise."""
    s64 = steam64(value)
    return f"[U:1:{int(s64) - STEAM64_BASE}]" if s64 else None


def log_steamids(text):
    ids = {}
    for line in text.splitlines():
        m = LOG_PLAYER.search(line)
        if not m:
            continue
        uid, sid, what = int(m.group(2)), m.group(3), m.group(4)
        if what == "disconnected":
            ids.pop(uid, None)
        elif sid.startswith("[U:"):
            ids[uid] = sid
    return ids


def parse_roster(status_text, steamids):
    rows = []
    in_players = False
    for line in status_text.splitlines():
        if line.startswith("---------players"):
            in_players = True
            continue
        if line.startswith("#end"):
            break
        m = ROSTER_ROW.match(line) if in_players else None
        if not m or m.group(1) == "65535":
            continue
        uid = int(m.group(1))
        bot = m.group(2) == "BOT"
        sid = steamids.get(uid)
        rows.append({"userid": uid, "name": m.group(8), "bot": bot, "steamid": sid, "steam64": steam64(sid) if sid else None,
                     "time": None if bot else m.group(2), "ping": int(m.group(3)), "loss": int(m.group(4)),
                     "state": m.group(5), "ip": (m.group(7).rsplit(":", 1)[0] if m.group(7) and not m.group(7).startswith("unknown") else None)})
    return rows


def cmd_players():
    answer = console("status")
    if answer is None:
        out({"running": False, "players": []})
        return
    rows = parse_roster(answer, log_steamids(console_tail(2_000_000)))
    out({"running": True, "players": rows, "humans": sum(1 for r in rows if not r["bot"]), "bots": sum(1 for r in rows if r["bot"])})


def clean_reason(text):
    return re.sub(r'["\;\r\n]', " ", text or "").strip()[:80]


def cmd_kick(userid, reason=""):
    if not userid.isdigit():
        fail("Expected the player's user id (the number in the roster).")
    why = clean_reason(reason)
    answer = console(f'kickid {userid} "{why}"' if why else f"kickid {userid}")
    if answer is None:
        fail("The server is not running.")
    out({"ok": "not found" not in answer, "answer": answer.strip()[-200:]})


def cmd_ban(who, minutes="0"):
    """Ban a SteamID (any form) or an IPv4 address, and write the list NOW. minutes 0 = for good.
    Timed bans live in memory only (the game does not write them down), permanent ones are in the file."""
    if not re.fullmatch(r"\d{1,7}", str(minutes)):
        fail("Minutes must be a number; 0 is permanent.")
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", who):
        if any(int(x) > 255 for x in who.split(".")):
            fail("Not a valid IPv4 address.")
        console(f"addip {minutes} {who}")
        console("writeip")
        out({"ok": True, "kind": "ip", "id": who, "minutes": int(minutes)})
        return
    u = steam_u(who)
    if not u:
        fail("Expected a SteamID64, [U:1:n], STEAM_x:y:z or an IPv4 address.")
    if console(f"banid {minutes} {u}") is None:
        # No server running: the list is the file, add the line there.
        line = f"banid {minutes} {u}"
        text = read(BANNED_USER) or ""
        if line not in text.splitlines() and minutes == "0":
            write_text(BANNED_USER, text + ("" if text.endswith("\n") or not text else "\n") + line + "\n")
    else:
        console("writeid")
    out({"ok": True, "kind": "id", "id": u, "steam64": steam64(u), "minutes": int(minutes)})


def cmd_unban(who):
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", who):
        console(f"removeip {who}")
        console("writeip")
        if console("status") is None:
            write_text(BANNED_IP, "".join(l + "\n" for l in (read(BANNED_IP) or "").splitlines() if not re.search(rf"\s{re.escape(who)}\s*$", l)))
        out({"ok": True, "kind": "ip", "id": who})
        return
    u = steam_u(who)
    if not u:
        fail("Expected a SteamID64, [U:1:n], STEAM_x:y:z or an IPv4 address.")
    if console(f"removeid {u}") is None:
        write_text(BANNED_USER, "".join(l + "\n" for l in (read(BANNED_USER) or "").splitlines() if not re.search(rf"\s{re.escape(u)}\s*$", l)))
    else:
        console("writeid")
    out({"ok": True, "kind": "id", "id": u})


def cmd_change_map(target):
    """A stock map, or a Workshop map by ID or link, now."""
    ws = re.search(r"[?&]id=(\d{4,20})", target) or re.fullmatch(r"(?:workshop/)?(\d{4,20})", target)
    if ws:
        command = f"host_workshop_map {ws.group(1)}"
    elif MAP_NAME.match(target):
        command = f"changelevel {target}"
    else:
        fail("Expected a map name or a Workshop map ID or link.")
    answer = console(command)
    if answer is None:
        fail("The server is not running.")
    out({"ok": True, "command": command, "answer": answer.strip()[-300:]})


def main(argv):
    commands = {
        "files": (cmd_files, 0), "set-mapcycle": (cmd_set_mapcycle, 1), "write-mapgroup": (cmd_write_mapgroup, 0),
        "plugin-install": (cmd_plugin_install, 1), "plugin-add": (cmd_plugin_add, 1),
        "plugin-enable": (cmd_plugin_enable, 2), "plugin-remove": (cmd_plugin_remove, 1),
        "set-admins": (cmd_set_admins, 1), "addons-retry": (cmd_addons_retry, 0),
        "backups": (cmd_backups, 0), "restore": (cmd_restore, 1),
        "backup": (cmd_backup, (1, 2)), "apply-restore": (cmd_apply_restore, 0),
        "state": (cmd_state, (1, 2)), "status": (cmd_status, 0), "probe": (cmd_probe, 0), "gslt-state": (cmd_gslt_state, 0),
        "settings": (cmd_settings, 0), "set-settings": (cmd_set_settings, 1), "players": (cmd_players, 0),
        "kick": (cmd_kick, (1, 2)), "ban": (cmd_ban, (1, 2)), "unban": (cmd_unban, 1), "change-map": (cmd_change_map, 1),
    }
    if not argv or argv[0] not in commands:
        print(__doc__, file=sys.stderr)
        return 64
    arity = commands[argv[0]][1]
    if len(argv) - 1 not in (arity if isinstance(arity, tuple) else (arity,)):
        print(__doc__, file=sys.stderr)
        return 64
    commands[argv[0]][0](*argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
