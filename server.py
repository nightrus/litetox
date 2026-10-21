"""litetox — minimal Wialon IPS server for ALTOX heater/tracker modules.

TCP (Wialon IPS 1.1/2.0) <- modules, HTTP JSON API -> Home Assistant (ha-litetox).
Only IMEIs registered in the `devices` table (enabled=1) may log in.
Everything is stored in MariaDB; current per-device state lives in memory and
is snapshotted to devices.state so it survives restarts.
"""
import asyncio
import json
import logging
import logging.handlers
import math
import os
import re
import threading
import time
from datetime import datetime, timezone

import pymysql
from aiohttp import web

LISTEN_TCP = int(os.environ.get("LISTEN_TCP", "20013"))
LISTEN_HTTP = int(os.environ.get("LISTEN_HTTP", "20080"))
API_TOKEN = os.environ.get("API_TOKEN", "")
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
STATUS_POLL = int(os.environ.get("STATUS_POLL", "600"))     # cmd_STATUS every 10 min
USSD_POLL = int(os.environ.get("USSD_POLL", "86400"))       # cmd_ussd once a day
RAW_RETENTION_DAYS = int(os.environ.get("RAW_RETENTION_DAYS", "30"))
ONLINE_TIMEOUT = int(os.environ.get("ONLINE_TIMEOUT", "180"))
# legacy /state and /cmd (no imei) map to this device; kept until HA is migrated
DEFAULT_IMEI = os.environ.get("DEFAULT_IMEI", "")

DB = dict(
    host=os.environ.get("DB_HOST", "mariadb"),
    port=int(os.environ.get("DB_PORT", "3306")),
    user=os.environ.get("DB_USER", "litetox"),
    password=os.environ.get("DB_PASSWORD", ""),
    database=os.environ.get("DB_NAME", "litetox"),
)

VERSION = "2.0.0"


# ---------------------------------------------------------------- logging
def log(*a):
    print(*a, flush=True)


_raw = logging.getLogger("altox")
_raw.setLevel(logging.INFO)
try:
    _rh = logging.handlers.RotatingFileHandler(
        os.path.join(DATA_DIR, "altox.log"), maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    _rh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _raw.addHandler(_rh)
except Exception as e:
    print("rawlog init err", e, flush=True)


def rawlog(msg):
    print(msg, flush=True)
    try:
        _raw.info(msg)
    except Exception:
        pass


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------- database
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS devices (
        imei VARCHAR(32) PRIMARY KEY,
        name VARCHAR(100) NOT NULL DEFAULT '',
        enabled TINYINT(1) NOT NULL DEFAULT 1,
        created_at DATETIME NOT NULL,
        last_seen DATETIME NULL,
        last_ip VARCHAR(64) NULL,
        state LONGTEXT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS positions (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        imei VARCHAR(32) NOT NULL,
        ts DATETIME NOT NULL,
        received_at DATETIME NOT NULL,
        source VARCHAR(4) NOT NULL,
        lat DOUBLE NULL, lon DOUBLE NULL,
        speed DOUBLE NULL, course DOUBLE NULL, altitude DOUBLE NULL,
        satellites INT NULL, hdop DOUBLE NULL,
        voltage DOUBLE NULL, cabin_temp DOUBLE NULL,
        input_bits VARCHAR(16) NULL, output_bits VARCHAR(16) NULL,
        params TEXT NULL,
        INDEX ix_pos (imei, ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS heater_status (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        imei VARCHAR(32) NOT NULL,
        received_at DATETIME NOT NULL,
        heater_on TINYINT(1) NULL, minutes_left INT NULL,
        heater_temp INT NULL, cabin_temp INT NULL,
        flame TINYINT(1) NULL, output_on TINYINT(1) NULL,
        voltage DOUBLE NULL, gsm INT NULL,
        satellites INT NULL, hdop DOUBLE NULL, speed DOUBLE NULL,
        raw TEXT NULL,
        INDEX ix_hs (imei, received_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS balance (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        imei VARCHAR(32) NOT NULL,
        received_at DATETIME NOT NULL,
        amount DOUBLE NULL,
        raw TEXT NULL,
        INDEX ix_bal (imei, received_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS commands (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        imei VARCHAR(32) NOT NULL,
        created_at DATETIME NOT NULL,
        sent_at DATETIME NULL,
        command VARCHAR(255) NOT NULL,
        origin VARCHAR(16) NOT NULL,
        status VARCHAR(16) NOT NULL,
        response_type VARCHAR(4) NULL,
        response_at DATETIME NULL,
        response TEXT NULL,
        INDEX ix_cmd (imei, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS raw_log (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        imei VARCHAR(32) NULL,
        ts DATETIME NOT NULL,
        direction CHAR(1) NOT NULL,
        peer VARCHAR(64) NULL,
        frame TEXT NOT NULL,
        INDEX ix_raw (ts), INDEX ix_raw_imei (imei, ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]


class Db:
    """One pymysql connection used from worker threads, serialized by a lock."""

    def __init__(self):
        self._lock = threading.Lock()
        self._conn = None

    def _connect(self):
        self._conn = pymysql.connect(
            **DB, charset="utf8mb4", autocommit=True,
            cursorclass=pymysql.cursors.DictCursor, connect_timeout=10)

    def _run(self, sql, args=None, fetch=None):
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._conn is None:
                        self._connect()
                    else:
                        try:
                            self._conn.ping()
                        except Exception:
                            self._connect()
                    with self._conn.cursor() as c:
                        c.execute(sql, args)
                        if fetch == "all":
                            return c.fetchall()
                        if fetch == "one":
                            return c.fetchone()
                        return c.lastrowid
                except pymysql.err.OperationalError:
                    self._conn = None
                    if attempt == 2:
                        raise

    async def q(self, sql, args=None, fetch=None):
        return await asyncio.to_thread(self._run, sql, args, fetch)

    async def all(self, sql, args=None):
        return await self.q(sql, args, "all")

    async def one(self, sql, args=None):
        return await self.q(sql, args, "one")


db = Db()


async def db_safe(coro):
    """Never let a DB hiccup kill a device session."""
    try:
        return await coro
    except Exception as e:
        log("db err", repr(e))
        return None


# ---------------------------------------------------------------- parsing
def haversine(a, b, c, d):
    p = math.radians
    x = math.sin(p(c - a) / 2) ** 2 + math.cos(p(a)) * math.cos(p(c)) * math.sin(p(d - b) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(x))


def dm_to_deg(v, hemi):
    try:
        v = float(v)
    except Exception:
        return None
    deg = int(v // 100)
    dd = deg + (v - deg * 100) / 60.0
    return round(-dd if hemi in ("S", "W") else dd, 6)


def num(x, c=float):
    try:
        return c(x)
    except Exception:
        return None


def parse_params(s):
    out = {}
    for part in s.split(","):
        bits = part.split(":")
        if len(bits) >= 3:
            out[bits[0]] = bits[2]
    return out


def parse_dt(d, t):
    try:
        return datetime.strptime(d + t, "%d%m%y%H%M%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def parse_record(rec):
    """Wialon short/full data record -> dict, or None."""
    f = rec.split(";")
    if len(f) < 10:
        return None
    r = {
        "ts": parse_dt(f[0], f[1]),
        "lat": dm_to_deg(f[2], f[3]),
        "lon": dm_to_deg(f[4], f[5]),
        "speed": num(f[6]),
        "course": num(f[7]),
        "altitude": num(f[8]),
        "satellites": num(f[9], int),
    }
    if len(f) >= 15:
        r.update(hdop=num(f[10]), input=f[11], output=f[12], voltage=num(f[13]))
        r["params"] = parse_params(f[15]) if len(f) > 15 else {}
        if "t" in r["params"]:
            r["cabin_temp"] = num(r["params"]["t"])
    return r


def parse_heater(text):
    def rx(p, cast=int):
        m = re.search(p, text)
        return cast(m.group(1)) if m else None

    gps = re.search(r"GPS:\s*(\d+)\s*,\s*([\d.]+)", text)
    return {
        "heater_on": "HEATER ON" in text,
        "minutes_left": rx(r"HEATER ON (\d+)\s*min") or 0,
        "heater_temp": rx(r"Temp heater:\s*(-?\d+)"),
        "cabin_temp": rx(r"Temp sensor:\s*(-?\d+)"),
        "flame": "Flame: yes" in text,
        "output_on": (rx(r"Output:\s*(\w+)", str) == "on") if "Output:" in text else None,
        "voltage": rx(r"Voltage:\s*([\d.]+)", float),
        "gsm": rx(r"GSM \(0-31\):\s*(\d+)"),
        "satellites": int(gps.group(1)) if gps else None,
        "hdop": float(gps.group(2)) if gps else None,
        "speed": rx(r"Speed:\s*([\d.]+)", float),
    }


def parse_ussd(text):
    m = re.search(r"(-?[0-9]+(?:[.,][0-9]+)?)\s*r\.", text)
    fw = re.search(r"Fw:([^ ;|\r\n]+)", text)
    return {
        "balance": float(m.group(1).replace(",", ".")) if m else None,
        "firmware": fw.group(1) if fw else None,
    }


def parse_cells(text):
    cells = []
    for m in re.finditer(r"(\d),(\d{3}),(\d{2,3}),([0-9a-fA-F]{4}),([0-9a-fA-F]{4})", text):
        if m.group(2) == "000":
            continue
        cells.append({"n": int(m.group(1)), "mcc": int(m.group(2)), "mnc": int(m.group(3)),
                      "lac": int(m.group(4), 16), "cid": int(m.group(5), 16)})
    return cells


# ---------------------------------------------------------------- devices
class Device:
    def __init__(self, imei, name="", state=None):
        self.imei = imei
        self.name = name
        self.state = state or {}
        self.writer = None
        self.peer = None
        self.last_seen = 0.0
        self.last_seen_db = 0.0
        self.queue = asyncio.Queue()
        self.login_at = 0.0
        self.next_status = 0.0
        self.dirty = False
        self.worker = asyncio.create_task(command_worker(self))

    @property
    def online(self):
        return self.writer is not None and time.time() - self.last_seen < ONLINE_TIMEOUT

    def public_state(self):
        s = {k: v for k, v in self.state.items() if not k.startswith("_")}
        s.update(imei=self.imei, name=self.name, online=self.online,
                 last_seen=iso(datetime.fromtimestamp(self.last_seen, timezone.utc).replace(microsecond=0))
                 if self.last_seen else self.state.get("last_seen"))
        return s


DEVICES: dict[str, Device] = {}


async def load_devices():
    rows = await db.all("SELECT imei, name, enabled, state, last_seen FROM devices")
    seen = set()
    for r in rows:
        if not r["enabled"]:
            continue
        seen.add(r["imei"])
        st = {}
        try:
            st = json.loads(r["state"]) if r["state"] else {}
        except Exception:
            pass
        if r["last_seen"]:
            st["last_seen"] = iso(r["last_seen"].replace(tzinfo=timezone.utc))
        d = DEVICES.get(r["imei"])
        if d:
            d.name = r["name"]
        else:
            DEVICES[r["imei"]] = Device(r["imei"], r["name"], st)
    for imei in list(DEVICES):
        if imei not in seen:
            await drop_device(imei)


async def drop_device(imei):
    d = DEVICES.pop(imei, None)
    if d:
        d.worker.cancel()
        if d.writer:
            d.writer.close()


async def save_state(d):
    d.dirty = False
    await db_safe(db.q("UPDATE devices SET state=%s WHERE imei=%s",
                       (json.dumps(d.state, ensure_ascii=False), d.imei)))


async def raw(imei, direction, peer, frame):
    rawlog(("< " if direction == "<" else "> ") + (f"[{imei}] " if imei else "") + frame)
    await db_safe(db.q("INSERT INTO raw_log (imei, ts, direction, peer, frame) VALUES (%s,%s,%s,%s,%s)",
                       (imei, utcnow(), direction, peer, frame[:4000])))


# ---------------------------------------------------------------- handlers per frame type
async def on_record(d, rec, source):
    r = parse_record(rec)
    if not r:
        return
    now = utcnow()
    st = d.state
    if r["lat"] is not None and r["lon"] is not None:
        ll, lo = st.get("_lastlat"), st.get("_lastlon")
        if ll is not None and lo is not None:
            dkm = haversine(ll, lo, r["lat"], r["lon"])
            if 0.005 < dkm < 5:
                st["odometer_km"] = round(st.get("odometer_km", 0) + dkm, 3)
        st["_lastlat"], st["_lastlon"] = r["lat"], r["lon"]
        st["latitude"], st["longitude"] = r["lat"], r["lon"]
    for k in ("speed", "course", "altitude", "satellites", "hdop", "voltage", "cabin_temp"):
        if r.get(k) is not None:
            st[k] = r[k]
    if "input" in r:
        st["input"], st["output"] = r["input"], r["output"]
    if "do2" in r.get("params", {}):
        st["out2"] = r["params"]["do2"]
    st["moving"] = bool(r.get("speed")) and r["speed"] > 2
    st["position_ts"] = iso(r["ts"] or now)
    d.dirty = True
    await db_safe(db.q(
        "INSERT INTO positions (imei, ts, received_at, source, lat, lon, speed, course, altitude, satellites,"
        " hdop, voltage, cabin_temp, input_bits, output_bits, params) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (d.imei, r["ts"] or now, now, source, r["lat"], r["lon"], r["speed"], r["course"], r["altitude"],
         r["satellites"], r.get("hdop"), r.get("voltage"), r.get("cabin_temp"), r.get("input"),
         r.get("output"), json.dumps(r.get("params") or {}))))


async def on_heater(d, text):
    h = parse_heater(text)
    now = utcnow()
    st = d.state
    st.update(heater_on=h["heater_on"], heater_minutes=h["minutes_left"], flame=h["flame"],
              heater_ts=iso(now), heater_raw=" | ".join(text.split("\n")[:10]))
    if h["heater_temp"] is not None:
        st["heater_temp"] = h["heater_temp"]
    if h["output_on"] is not None:
        st["heater_output"] = h["output_on"]
    for k in ("cabin_temp", "voltage", "gsm", "satellites", "hdop"):
        if h[k] is not None:
            st[k] = h[k]
    d.dirty = True
    await db_safe(db.q(
        "INSERT INTO heater_status (imei, received_at, heater_on, minutes_left, heater_temp, cabin_temp, flame,"
        " output_on, voltage, gsm, satellites, hdop, speed, raw) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (d.imei, now, h["heater_on"], h["minutes_left"], h["heater_temp"], h["cabin_temp"], h["flame"],
         h["output_on"], h["voltage"], h["gsm"], h["satellites"], h["hdop"], h["speed"], text)))
    await answer_command(d, "W", text)


async def on_ussd(d, text):
    u = parse_ussd(text)
    now = utcnow()
    st = d.state
    st["ussd_raw"] = text.replace("\n", " ")
    st["ussd_ts"] = iso(now)
    if u["balance"] is not None:
        st["balance"] = u["balance"]
        st["balance_ts"] = iso(now)
    if u["firmware"]:
        st["firmware"] = u["firmware"]
    d.dirty = True
    await db_safe(db.q("INSERT INTO balance (imei, received_at, amount, raw) VALUES (%s,%s,%s,%s)",
                       (d.imei, now, u["balance"], text)))
    await answer_command(d, "U", text)


async def on_cells(d, text):
    cells = parse_cells(text)
    now = utcnow()
    if cells:
        c = cells[0]
        d.state["gsm_cell"] = f"{c['mcc']}-{c['mnc']:02d} LAC:{c['lac']} CID:{c['cid']}"
    d.state["gsm_cells"] = cells
    d.state["cells_ts"] = iso(now)
    d.dirty = True
    await answer_command(d, "C", text)


async def answer_command(d, rtype, text):
    """Attach a module reply to the latest sent, unanswered command (within 3 min)."""
    await db_safe(db.q(
        "UPDATE commands SET status='answered', response_type=%s, response_at=%s, response=%s "
        "WHERE imei=%s AND status='sent' AND sent_at > %s ORDER BY id DESC LIMIT 1",
        (rtype, utcnow(), text, d.imei, datetime.fromtimestamp(time.time() - 180, timezone.utc))))


# ---------------------------------------------------------------- commands
def normalize_cmd(cmd):
    cmd = (cmd or "").strip()
    if not cmd or len(cmd) > 200 or "\r" in cmd or "\n" in cmd:
        return None
    return cmd if cmd.lower().startswith("cmd_") else "cmd_" + cmd


async def enqueue(d, cmd, origin):
    cid = await db_safe(db.q(
        "INSERT INTO commands (imei, created_at, command, origin, status) VALUES (%s,%s,%s,%s,'queued')",
        (d.imei, utcnow(), cmd, origin)))
    await d.queue.put((cid, cmd))
    return cid


async def command_worker(d):
    """Send queued commands one by one. An active USSD session swallows the next
    command, so keep 45 s after cmd_ussd and 5 s after anything else."""
    while True:
        cid, cmd = await d.queue.get()
        for _ in range(30):          # ride out a short reconnect
            if d.writer:
                break
            await asyncio.sleep(2)
        if not d.writer:
            await db_safe(db.q("UPDATE commands SET status='failed' WHERE id=%s", (cid,)))
            continue
        try:
            d.writer.write((cmd + "\r\n").encode())
            await d.writer.drain()
            await raw(d.imei, ">", d.peer, cmd)
            await db_safe(db.q("UPDATE commands SET status='sent', sent_at=%s WHERE id=%s", (utcnow(), cid)))
        except Exception as e:
            log("write err", d.imei, e)
            await db_safe(db.q("UPDATE commands SET status='failed' WHERE id=%s", (cid,)))
        if cmd.lower().startswith("cmd_ussd"):
            await asyncio.sleep(45)
        else:
            await asyncio.sleep(5)


async def poller():
    """Periodic cmd_STATUS / cmd_ussd for online devices. Schedules survive
    reconnects (the module re-logs in every few minutes on mobile networks)."""
    while True:
        await asyncio.sleep(15)
        now = time.time()
        for d in list(DEVICES.values()):
            if not d.online or now - d.login_at < 25:
                continue
            if now >= d.next_status:
                d.next_status = now + STATUS_POLL
                await enqueue(d, "cmd_STATUS", "poller")
            if now - d.state.get("_ussd_epoch", 0) >= USSD_POLL:
                d.state["_ussd_epoch"] = now
                await enqueue(d, "cmd_ussd", "poller")
                await save_state(d)


# ---------------------------------------------------------------- TCP session
def parse_login(body):
    """#L# body: v1.1 'imei;password' or v2.0 '2.0;imei;password;crc'."""
    p = body.split(";")
    if len(p) >= 3 and p[0] == "2.0":
        return p[1]
    return p[0]


async def handle(reader, writer):
    peer_t = writer.get_extra_info("peername") or ("?", 0)
    peer = f"{peer_t[0]}:{peer_t[1]}"
    log("connected", peer)
    d = None
    buf = b""
    try:
        while True:
            data = await asyncio.wait_for(reader.read(4096), timeout=ONLINE_TIMEOUT + 120)
            if not data:
                break
            buf += data
            if len(buf) > 65536:
                log("oversized buffer, drop", peer)
                break
            while b"\r\n" in buf:
                frame, buf = buf.split(b"\r\n", 1)
                s = frame.decode("latin1", errors="replace")
                if d is None:
                    if not s.startswith("#L#"):
                        await raw(None, "<", peer, s)
                        raise ConnectionAbortedError("data before login")
                    imei = parse_login(s[3:])
                    dev = DEVICES.get(imei)
                    await raw(imei if dev else None, "<", peer, s)
                    if not dev:
                        writer.write(b"#AL#0\r\n")
                        await writer.drain()
                        raise ConnectionAbortedError(f"unknown imei {imei!r}")
                    d = dev
                    if d.writer and d.writer is not writer:
                        d.writer.close()
                    d.writer, d.peer = writer, peer
                    d.last_seen = d.login_at = time.time()
                    await db_safe(db.q("UPDATE devices SET last_seen=%s, last_ip=%s WHERE imei=%s",
                                       (utcnow(), peer_t[0], imei)))
                    writer.write(b"#AL#1\r\n")
                    await writer.drain()
                    log("login ok", imei, peer)
                    continue
                d.last_seen = time.time()
                await raw(d.imei, "<", peer, s)
                ack = await route(d, s)
                if ack:
                    writer.write(ack.encode())
                    await writer.drain()
                if d.dirty:
                    await save_state(d)
                if time.time() - d.last_seen_db > 60:
                    d.last_seen_db = time.time()
                    await db_safe(db.q("UPDATE devices SET last_seen=%s WHERE imei=%s", (utcnow(), d.imei)))
    except ConnectionAbortedError as e:
        log("reject", peer, e)
    except asyncio.TimeoutError:
        log("idle timeout", peer)
    except Exception as e:
        log("conn err", peer, repr(e))
    finally:
        writer.close()
        if d and d.writer is writer:
            d.writer = None
            await save_state(d)
        log("closed", peer)


async def route(d, s):
    if s.startswith("#P#"):
        return "#AP#\r\n"
    if s.startswith("#SD#"):
        await on_record(d, s[4:], "SD")
        return "#ASD#1\r\n"
    if s.startswith("#D#"):
        await on_record(d, s[3:], "D")
        return "#AD#1\r\n"
    if s.startswith("#B#"):
        recs = [r for r in s[3:].split("|") if r]
        for r in recs:
            await on_record(d, r, "B")
        return f"#AB#{len(recs)}\r\n"
    # ALTOX text frames (newlines inside a frame arrive as bare \n)
    if s.startswith("#W#"):
        await on_heater(d, s[3:])
        return "#AM#1\r\n"
    if s.startswith("#U#"):
        await on_ussd(d, s[3:])
        return "#AM#1\r\n"
    if s.startswith("#C#"):
        await on_cells(d, s[3:])
        return "#AM#1\r\n"
    if s.startswith("#M#"):
        return "#AM#1\r\n"
    return None


# ---------------------------------------------------------------- HTTP API
IMEI_RE = re.compile(r"^[0-9A-Za-z_-]{4,32}$")


@web.middleware
async def auth_mw(request, handler):
    if request.path != "/health" and API_TOKEN:
        if request.headers.get("Authorization", "") != f"Bearer {API_TOKEN}":
            return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


def dev_or_404(imei):
    d = DEVICES.get(imei)
    if not d:
        raise web.HTTPNotFound(text=json.dumps({"error": "unknown device"}), content_type="application/json")
    return d


async def h_health(request):
    return web.json_response({"ok": True, "version": VERSION})


async def h_devices(request):
    rows = await db.all("SELECT imei, name, enabled, created_at, last_seen, last_ip FROM devices ORDER BY created_at")
    for r in rows:
        r["enabled"] = bool(r["enabled"])
        r["online"] = bool(DEVICES.get(r["imei"]) and DEVICES[r["imei"]].online)
        for k in ("created_at", "last_seen"):
            r[k] = iso(r[k].replace(tzinfo=timezone.utc)) if r[k] else None
    return web.json_response({"devices": rows, "version": VERSION})


async def h_register(request):
    body = await request.json()
    imei = str(body.get("imei", "")).strip()
    name = str(body.get("name", "")).strip()[:100]
    if not IMEI_RE.match(imei):
        return web.json_response({"error": "bad imei"}, status=400)
    await db.q("INSERT INTO devices (imei, name, enabled, created_at) VALUES (%s,%s,1,%s) "
               "ON DUPLICATE KEY UPDATE enabled=1, name=IF(%s='', name, %s)",
               (imei, name, utcnow(), name, name))
    await load_devices()
    return web.json_response({"ok": True, "device": DEVICES[imei].public_state()})


async def h_unregister(request):
    imei = request.match_info["imei"]
    await db.q("UPDATE devices SET enabled=0 WHERE imei=%s", (imei,))
    await load_devices()
    return web.json_response({"ok": True})


async def h_state(request):
    imei = request.match_info.get("imei") or DEFAULT_IMEI
    return web.json_response(dev_or_404(imei).public_state())


async def h_cmd(request):
    imei = request.match_info.get("imei") or DEFAULT_IMEI
    d = dev_or_404(imei)
    try:
        body = await request.json()
    except Exception:
        body = {}
    cmd = normalize_cmd(body.get("command"))
    if not cmd:
        return web.json_response({"error": "bad command"}, status=400)
    if not d.online:
        return web.json_response({"queued": False, "error": "device offline", "command": cmd}, status=409)
    cid = await enqueue(d, cmd, "api")
    return web.json_response({"queued": True, "id": cid, "command": cmd, "sent": True})


async def h_command(request):
    d = dev_or_404(request.match_info["imei"])
    r = await db.one("SELECT * FROM commands WHERE id=%s AND imei=%s", (int(request.match_info["id"]), d.imei))
    if not r:
        raise web.HTTPNotFound()
    for k in ("created_at", "sent_at", "response_at"):
        r[k] = iso(r[k].replace(tzinfo=timezone.utc)) if r[k] else None
    return web.json_response(r)


HISTORY = {
    "positions": ("positions", "ts"),
    "heater": ("heater_status", "received_at"),
    "balance": ("balance", "received_at"),
    "commands": ("commands", "created_at"),
}


async def h_history(request):
    d = dev_or_404(request.match_info["imei"])
    kind = request.query.get("type", "positions")
    if kind not in HISTORY:
        return web.json_response({"error": "bad type"}, status=400)
    table, col = HISTORY[kind]
    limit = max(1, min(int(request.query.get("limit", 500)), 5000))
    since = request.query.get("since")
    args = [d.imei]
    where = "imei=%s"
    if since:
        where += f" AND {col} >= %s"
        args.append(datetime.fromisoformat(since).astimezone(timezone.utc).replace(tzinfo=None))
    rows = await db.all(f"SELECT * FROM {table} WHERE {where} ORDER BY {col} DESC LIMIT {limit}", args)
    for r in rows:
        for k, v in r.items():
            if isinstance(v, datetime):
                r[k] = iso(v.replace(tzinfo=timezone.utc))
    return web.json_response({"type": kind, "items": rows})


# ---------------------------------------------------------------- housekeeping
async def housekeeping():
    while True:
        await db_safe(db.q("DELETE FROM raw_log WHERE ts < %s",
                           (datetime.fromtimestamp(time.time() - RAW_RETENTION_DAYS * 86400, timezone.utc),)))
        await db_safe(db.q("UPDATE commands SET status='expired' WHERE status IN ('queued','sent') AND created_at < %s",
                           (datetime.fromtimestamp(time.time() - 3600, timezone.utc),)))
        await asyncio.sleep(6 * 3600)


async def migrate_legacy():
    """One-time import of v1 state.json (odometer + last position) into DEFAULT_IMEI."""
    path = os.path.join(DATA_DIR, "state.json")
    if not DEFAULT_IMEI or not os.path.exists(path):
        return
    try:
        with open(path) as f:
            old = json.load(f)
    except Exception:
        return
    await db.q("INSERT IGNORE INTO devices (imei, name, enabled, created_at) VALUES (%s,'',1,%s)",
               (DEFAULT_IMEI, utcnow()))
    row = await db.one("SELECT state FROM devices WHERE imei=%s", (DEFAULT_IMEI,))
    st = json.loads(row["state"]) if row and row["state"] else {}
    if "odometer_km" not in st:
        st["odometer_km"] = old.get("odometer_km", 0)
        st["_lastlat"], st["_lastlon"] = old.get("_lastlat"), old.get("_lastlon")
        if old.get("_lastlat") is not None:
            st["latitude"], st["longitude"] = old["_lastlat"], old["_lastlon"]
        await db.q("UPDATE devices SET state=%s WHERE imei=%s", (json.dumps(st), DEFAULT_IMEI))
    os.rename(path, path + ".migrated")
    log("migrated legacy state.json ->", DEFAULT_IMEI)


async def wait_db():
    for i in range(60):
        try:
            for sql in SCHEMA:
                await db.q(sql)
            return
        except Exception as e:
            log("waiting for db:", repr(e))
            await asyncio.sleep(3)
    raise SystemExit("database unavailable")


async def main():
    await wait_db()
    await migrate_legacy()
    await load_devices()
    log("devices:", ", ".join(DEVICES) or "none")

    app = web.Application(middlewares=[auth_mw])
    app.add_routes([
        web.get("/health", h_health),
        web.get("/devices", h_devices),
        web.post("/devices", h_register),
        web.delete("/devices/{imei}", h_unregister),
        web.get("/state/{imei}", h_state),
        web.post("/cmd/{imei}", h_cmd),
        web.get("/cmd/{imei}/{id:\\d+}", h_command),
        web.get("/history/{imei}", h_history),
        web.get("/state", h_state),       # legacy
        web.post("/cmd", h_cmd),          # legacy
    ])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", LISTEN_HTTP).start()

    asyncio.create_task(housekeeping())
    asyncio.create_task(poller())
    srv = await asyncio.start_server(handle, "0.0.0.0", LISTEN_TCP)
    log(f"litetox {VERSION} tcp:{LISTEN_TCP} http:{LISTEN_HTTP} poll:{STATUS_POLL}s ussd:{USSD_POLL}s")
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
