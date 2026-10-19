#!/usr/bin/env python3
"""Unit tests for scripts/fluxcs2.py: parsers, validation and the A2S exchange, against canned data."""
import importlib.util
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update({"FLUX_DATA_DIR": TMP + "/data", "FLUX_SERVER_DIR": TMP + "/server", "FLUX_STATE_FILE": TMP + "/state.json",
                   "FLUX_GENERATION_FILE": TMP + "/gen", "FLUX_RESTART_MARKER": TMP + "/restart", "FLUX_CONSOLE_CMD": "/nonexistent",
                   "FLUX_STEAM_CACHE": TMP + "/steam.json"})
spec = importlib.util.spec_from_file_location("fluxcs2", os.path.join(ROOT, "scripts", "fluxcs2.py"))
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)

STATUS = """----- Status -----
hostname : Flux
version  : 1.41.9.1/14191 10924 secure  public
steamid  : [G:1:2376461328:51984] (90295264503786512)
---------players--------
  id     time ping loss      state   rate adr name
65535 [NoChan]    0    0 challenging      0unknown ''
   2      BOT    0    0     active      0 'Mangos'
   3   01:23   35    0     active 786432 203.0.113.7:27005 'Real Player'
   4   1:02:03  120    2     active 786432 198.51.100.9:27005 'Name With ''quotes'''
#end
"""
LOG = '''L 10/19/2026 - 19:21:28: "Mangos<2><BOT><>" connected, address "(unknown)"
L 10/19/2026 - 19:22:01: "Real Player<3><[U:1:12345]><>" connected, address "203.0.113.7:27005"
L 10/19/2026 - 19:22:09: "Real Player<3><[U:1:12345]><Unassigned>" entered the game
L 10/19/2026 - 19:22:10: "Gone<5><[U:1:999]><CT>" entered the game
L 10/19/2026 - 19:22:11: "Gone<5><[U:1:999]><CT>" disconnected (reason "NETWORK_DISCONNECT_KICKED")
L 10/19/2026 - 19:22:12: "Late<4><STEAM_ID_PENDING><>" connected, address "198.51.100.9:27005"
L 10/19/2026 - 19:22:13: "Name With 'quotes'<4><[U:1:76561197960265729]><>" entered the game
'''


def run(fn, *args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            fn(*args)
        except SystemExit:
            pass
    return json.loads(buf.getvalue())


class SteamIds(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(f.steam64("[U:1:12345]"), "76561197960278073")
        self.assertEqual(f.steam64("STEAM_0:1:6172"), "76561197960278073")
        self.assertEqual(f.steam64("76561197960278073"), "76561197960278073")
        self.assertIsNone(f.steam64("hello"))
        self.assertEqual(f.steam_u("76561197960278073"), "[U:1:12345]")
        self.assertIsNone(f.steam_u("1.2.3.4"))


class Roster(unittest.TestCase):
    def test_roster_joins_status_and_log(self):
        rows = f.parse_roster(STATUS, f.log_steamids(LOG))
        self.assertEqual([r["userid"] for r in rows], [2, 3, 4])
        bot, human = rows[0], rows[1]
        self.assertTrue(bot["bot"])
        self.assertIsNone(bot["steamid"])
        self.assertEqual(human["steamid"], "[U:1:12345]")
        self.assertEqual(human["steam64"], "76561197960278073")
        self.assertEqual(human["ip"], "203.0.113.7")
        self.assertEqual(human["ping"], 35)
        self.assertEqual(rows[2]["steamid"], "[U:1:76561197960265729]")

    def test_disconnected_player_is_forgotten(self):
        self.assertNotIn(5, f.log_steamids(LOG))

    def test_pending_id_is_not_an_id(self):
        self.assertNotIn(4, f.log_steamids(LOG.splitlines(True)[5]))


class Settings(unittest.TestCase):
    def d(self, key):
        return f.SETTING_BY_KEY[key]

    def test_values(self):
        self.assertEqual(f.setting_value(self.d("mp_roundtime"), "2")[0], "2.0")
        self.assertEqual(f.setting_value(self.d("mp_freezetime"), 10)[0], "10")
        self.assertEqual(f.setting_value(self.d("mp_friendlyfire"), True)[0], "1")
        self.assertEqual(f.setting_value(self.d("mp_friendlyfire"), "off")[0], "0")
        self.assertEqual(f.setting_value(self.d("bot_quota_mode"), "fill")[0], "fill")

    def test_refused(self):
        self.assertIsNotNone(f.setting_value(self.d("mp_roundtime"), "0")[1])
        self.assertIsNotNone(f.setting_value(self.d("mp_freezetime"), "abc")[1])
        self.assertIsNotNone(f.setting_value(self.d("bot_quota_mode"), "x; quit")[1])
        self.assertIsNotNone(f.setting_value(self.d("sv_cheats"), "maybe")[1])
        self.assertIsNotNone(f.setting_value(self.d("mp_startmoney"), "99999999")[1])

    def test_every_default_is_valid_for_its_own_definition(self):
        for d in f.SETTING_DEFS:
            value, why = f.setting_value(d, d["default"])
            self.assertIsNone(why, f"{d['key']}: {why}")

    def test_set_settings_writes_and_resets(self):
        run(f.cmd_set_settings, json.dumps({"mp_roundtime": "3", "bot_quota": 5, "evil": "1"}))
        text = open(f.SETTINGS_FILE).read()
        self.assertIn("mp_roundtime 3.0", text)
        self.assertIn("bot_quota 5", text)
        self.assertNotIn("evil", text)
        r = run(f.cmd_set_settings, json.dumps({"mp_roundtime": None}))
        self.assertEqual(r["reset"], ["mp_roundtime"])
        self.assertNotIn("mp_roundtime", open(f.SETTINGS_FILE).read())
        self.assertEqual(r["applied"], [])  # no server running here


class Bans(unittest.TestCase):
    def test_parse(self):
        rows = f.parse_bans("banid 0 [U:1:12345]\nbanid 30 76561197960278073\n", "addip 0 1.2.3.4\n")
        self.assertEqual([r["kind"] for r in rows], ["id", "id", "ip"])
        self.assertEqual(rows[0]["steam64"], "76561197960278073")

    def test_ban_with_no_server_writes_the_file(self):
        os.makedirs(f.DATA, exist_ok=True)
        f.BANNED_USER = os.path.join(f.DATA, "banned_user.cfg")
        r = run(f.cmd_ban, "76561197960278073", "0")
        self.assertEqual(r["id"], "[U:1:12345]")
        self.assertEqual(open(f.BANNED_USER).read(), "banid 0 [U:1:12345]\n")
        run(f.cmd_ban, "76561197960278073", "0")
        self.assertEqual(open(f.BANNED_USER).read().count("banid"), 1)
        run(f.cmd_unban, "[U:1:12345]")
        self.assertEqual(open(f.BANNED_USER).read(), "")

    def test_bad_input(self):
        self.assertFalse(run(f.cmd_ban, "not a steam id")["ok"])
        self.assertFalse(run(f.cmd_ban, "999.1.1.1")["ok"])
        self.assertFalse(run(f.cmd_ban, "76561197960278073", "-5")["ok"])
        self.assertFalse(run(f.cmd_kick, "1; quit")["ok"])
        self.assertFalse(run(f.cmd_change_map, "de_dust2; quit")["ok"])


class Steam(unittest.TestCase):
    def test_login_states(self):
        self.assertEqual(f.steam_login("steamid  : [G:1:2376461328:51984] (9)\nversion : 1 secure public"), "ok")
        self.assertEqual(f.steam_login("steamid  : [A:1:2376461328:51984] (9)"), "anonymous")
        self.assertEqual(f.steam_login("version : insecure (secure mode enabled, disconnected from Steam3)\nsteamid  : [I:0:1] (1)"), "failed")
        self.assertIsNone(f.steam_login("nothing"))

    def test_token_shape(self):
        os.environ["CS2_GSLT"] = ""
        self.assertEqual(f.gslt_state(), "missing")
        os.environ["CS2_GSLT"] = "xyz"
        self.assertEqual(f.gslt_state(), "invalid")
        os.environ["CS2_GSLT"] = "0123456789abcdef0123456789ABCDEF"
        self.assertEqual(f.gslt_state(), "set")


class State(unittest.TestCase):
    def test_state_and_status_without_a_server(self):
        os.environ["GAME_PORT"] = "1"  # nothing answers there
        f.cmd_state("needs-fix", "GAME_PORT is bad")
        r = run(f.cmd_status)
        self.assertEqual((r["state"], r["message"], r["query"]), ("needs-fix", "GAME_PORT is bad", False))
        self.assertIsNone(r["runningSince"])
        f.cmd_state("running")
        self.assertIsNotNone(run(f.cmd_status)["runningSince"])


class A2S(unittest.TestCase):
    def test_challenge_exchange(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        head = b"\xff\xff\xff\xff"
        info = b"I\x11" + b"Flux\x00de_dust2\x00csgo\x00Counter-Strike 2\x00" + struct.pack("<H", 730) + bytes([3, 16, 2]) + b"dl\x00\x00" + b"1.41.9.1\x00"
        players = b"D\x01" + b"\x00Alice\x00" + struct.pack("<lf", 7, 61.0)

        def serve():
            for _ in range(4):
                data, addr = sock.recvfrom(1024)
                if data.endswith(b"\xff\xff\xff\xff") or data == head + b"TSource Engine Query\x00":
                    sock.sendto(head + b"A\x01\x02\x03\x04", addr)  # challenge
                elif data.startswith(head + b"T"):
                    sock.sendto(head + info, addr)
                else:
                    sock.sendto(head + players, addr)

        threading.Thread(target=serve, daemon=True).start()
        os.environ["GAME_PORT"] = str(port)
        i, p = f.query()
        sock.close()
        self.assertEqual((i["name"], i["map"], i["players"], i["maxPlayers"], i["bots"]), ("Flux", "de_dust2", 3, 16, 2))
        self.assertEqual(p, [{"name": "Alice", "score": 7, "seconds": 61}])


if __name__ == "__main__":
    unittest.main(verbosity=1)
