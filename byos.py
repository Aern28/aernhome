# created-by: fable
# created: 2026-09-02
# purpose: BYOS - Nexus serves the TRMNL e-ink display directly (device API, renderer, wall/pin control)
# lifespan: infrastructure
# project: trmnl-byos
"""byos.py — Bring Your Own Server for the TRMNL OG on the wall.

Aern, 2026-09-02: "something like BYOS so you could run our plugins directly and
I could do cmds like 'pull up this page' — the random q15min refresh is less
useful than something targeted and controllable." So the device points at Nexus
instead of trmnl.com, the plugin markups in byos_templates/ render here, and a
`wall:` verb (Aernbot / curl) pins any screen for N minutes.

Two Flask surfaces:
  device_bp  — what the DEVICE calls. Served on its own LAN-bound port (5556) by
               app.py, because Nexus proper is Tailscale-only on purpose and the
               TRMNL is Wi-Fi-only. Routes: /api/setup, /api/display, /api/log,
               /byos/img/<file>. Auth = the api_key WE issue at setup.
  nexus_bp   — the control surface, on Nexus (Tailscale-only): /api/wall
               (GET status / POST pin|clear|mode|playlist), /byos/screen/<name>
               (the HTML the renderer screenshots; also a browser preview),
               /byos/preview/<name>.png (the exact 1-bit image the device gets).

Firmware contract (docs.trmnl.com/go/diy/byos, terminus reference server):
  GET /api/setup   headers ID (MAC), FW-Version, Model
                   -> {status:200, api_key, friendly_id, image_url, message}
  GET /api/display headers ID, Access-Token, Battery-Voltage, RSSI, FW-Version,
                   Refresh-Rate, Update-Source timer|button|powercycle, ...
                   -> {status:0, image_url, filename, refresh_rate, update_firmware:false,
                       firmware_url:null, reset_firmware:false, special_function:"none"}
                   `filename` is the device's cache key: same filename -> it skips the
                   repaint, so the name carries a content hash.
  POST /api/log    -> 204
Image: 800x480 1-bit. PNG (Content-Type: image/png, FW >= 1.5.2) or BMP for older
firmware; both well under the OG's 90 kB no-PSRAM ceiling.

Renderer: headless Chrome over raw CDP (websocket-client) against the existing
`playwright-chrome-relay` sockpuppetbrowser on relay-net — no browser in this
image. Screenshot -> Pillow -> Floyd-Steinberg 1-bit. If Chrome is unreachable the
device still gets a Pillow-drawn text screen saying so, never a stale/blank wall.

Refresh policy (server-set, the device obeys): on battery idle 15 min / pinned 2 min;
"desk" mode (USB — the OG sends no USB header, so a steady >= 4.15 V over 3 polls,
or `mode: desk` by hand) idle 60 s / pinned 30 s. A short button press wakes the
device and fetches immediately, so "wall: X" + a tap is instant on battery too.
"""
import base64
import datetime
import hashlib
import io
import itertools
import json
import os
import re
import secrets
import tempfile
import threading
import time
from zoneinfo import ZoneInfo

from flask import Blueprint, Flask, Response, abort, jsonify, request, send_from_directory

CT = ZoneInfo("America/Chicago")
DATA_DIR = os.environ.get("DATA_DIR", "C:/projects/aernhome/data")
BYOS_DIR = os.path.join(DATA_DIR, "byos")
IMG_DIR = os.path.join(BYOS_DIR, "img")
STATE_PATH = os.path.join(BYOS_DIR, "state.json")
# What the DEVICE can reach (LAN-bound device listener) — image_url is built from this.
BASE_URL = os.environ.get("BYOS_BASE_URL", "http://192.168.1.141:5556").rstrip("/")
# What the RENDERER (Chrome container on relay-net) can reach to fetch screen HTML.
INTERNAL_URL = os.environ.get("BYOS_INTERNAL_URL", "http://aernhome-dashboard:5555").rstrip("/")
CDP_URL = os.environ.get("BYOS_CDP_URL", "ws://playwright-chrome-relay:3000")
TEMPLATE_DIRS = [d for d in (os.environ.get("BYOS_PLUGIN_DIR", ""),
                             os.path.join(os.path.dirname(os.path.abspath(__file__)), "byos_templates")) if d]
FAMILY_BOARD_PATH = os.environ.get("FAMILY_BOARD_PATH", "/tcg/family-board.json")
W, H = 800, 480
ROTATE_S = int(os.environ.get("BYOS_ROTATE_S", "900"))
DEFAULT_PLAYLIST = ["weather", "agenda", "restock", "dashboard", "season", "tcg"]
REFRESH = {"battery": {"idle": 900, "wall": 120}, "desk": {"idle": 60, "wall": 30}}
MIN_REFRESH = 20
DESK_VOLTS, DESK_N = 4.15, 3
RENDER_TTL_S = 25          # same screen asked again inside this window reuses the last render
IMG_KEEP_S = 2 * 3600      # prune rendered images older than this
MAX_WALL_MIN = 12 * 60
OPEN_METEO = ("https://api.open-meteo.com/v1/forecast?latitude=29.76&longitude=-95.36"
              "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,"
              "wind_direction_10m,uv_index&hourly=temperature_2m,precipitation_probability,weather_code"
              "&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset&temperature_unit=fahrenheit"
              "&wind_speed_unit=mph&timezone=America/Chicago&forecast_days=3")
GWL_DATE = datetime.date(2026, 10, 9)

device_bp = Blueprint("byos_device", __name__)
nexus_bp = Blueprint("byos_nexus", __name__)
_lock = threading.RLock()
_render_cache = {}   # screen -> {"at", "png", "html_err"}
_weather_cache = {"at": 0.0, "data": None}


# ── state ─────────────────────────────────────────────────────────────────────
def _now():
    return time.time()


def _iso(ts=None):
    return datetime.datetime.fromtimestamp(ts or _now(), CT).isoformat(timespec="seconds")


def _default_state():
    return {"devices": {}, "wall": None, "mode": "auto", "playlist": list(DEFAULT_PLAYLIST),
            "log": [], "errors": [], "last_display": None}


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
        base = _default_state()
        base.update(st if isinstance(st, dict) else {})
        return base
    except (OSError, json.JSONDecodeError):
        return _default_state()


def save_state(st):
    os.makedirs(BYOS_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=BYOS_DIR, prefix=".state-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def _note_error(st, where, err):
    st.setdefault("errors", []).append({"at": _iso(), "where": where, "error": str(err)[:300]})
    st["errors"] = st["errors"][-30:]


# ── templates + data ───────────────────────────────────────────────────────────
SHELL = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://usetrmnl.com/css/latest/plugins.css">
<script src="https://usetrmnl.com/js/latest/plugins.js"></script>
<style>html,body{margin:0;padding:0;width:800px;height:480px;overflow:hidden;background:#fff;color:#000}
.screen{width:800px;height:480px;overflow:hidden}</style></head>
<body class="environment trmnl"><div class="screen">%s</div></body></html>"""


def _liquid_env():
    from liquid import Environment, FileSystemLoader
    return Environment(loader=FileSystemLoader([d for d in TEMPLATE_DIRS if os.path.isdir(d)]))


def data_weather():
    if _now() - _weather_cache["at"] < 600 and _weather_cache["data"]:
        return _weather_cache["data"]
    from urllib.request import Request, urlopen
    with urlopen(Request(OPEN_METEO, headers={"User-Agent": "aernhome byos"}), timeout=20) as r:
        d = json.loads(r.read().decode("utf-8"))
    _weather_cache.update(at=_now(), data=d)
    return d


def data_agenda():
    """family-board-push.js (relay) writes the Nest Hub board JSON to C:/tcg-inventory/
    family-board.json (= /tcg here); its `attributes` are exactly the TRMNL merge vars."""
    with open(FAMILY_BOARD_PATH, "r", encoding="utf-8") as f:
        d = json.load(f)
    return d.get("attributes", d)


def data_restock():
    import trmnl_push
    return trmnl_push.build_restock()["merge_variables"]


def data_dashboard():
    import trmnl_push
    return trmnl_push.build_dashboard()["merge_variables"]


def data_season():
    import trmnl_push
    return trmnl_push.build_season()["merge_variables"]


def data_tcg():
    import trmnl_push
    return trmnl_push.build_tcg()["merge_variables"]


def data_gwl():
    days = (GWL_DATE - datetime.date.today()).days
    return {"days": days, "weeks_part": days // 7, "days_part": days % 7,
            "updated": datetime.datetime.now(CT).strftime("%b %d %I:%M %p").replace(" 0", " ")}


SCREENS = {
    "weather": ("weather-openmeteo.html", data_weather, "Weather (Open-Meteo, Houston)"),
    "agenda": ("family-agenda.html", data_agenda, "Family week agenda"),
    "restock": ("restock.html", data_restock, "Household restock list"),
    "dashboard": ("dashboard.html", data_dashboard, "Aern dashboard v2 (services + storage)"),
    "season": ("seasons.html", data_season, "72 Seasons"),
    "tcg": ("tcg-business.html", data_tcg, "TCG business"),
    "gwl": ("gwl-countdown.html", data_gwl, "Great Wolf Lodge countdown"),
}


def screen_html(name, text=None):
    """Render a screen's HTML (Liquid markup inside the TRMNL framework shell)."""
    env = _liquid_env()
    if name == "text":
        tpl = env.get_template("text.html")
        return SHELL % tpl.render(msg=text or "", updated=datetime.datetime.now(CT).strftime("%I:%M %p").lstrip("0"))
    if name not in SCREENS:
        raise KeyError(name)
    tpl_name, fn, _ = SCREENS[name]
    return SHELL % env.get_template(tpl_name).render(**fn())


# ── renderer ──────────────────────────────────────────────────────────────────
def _cdp_ws_url():
    """ws:// is used as-is (sockpuppetbrowser); http(s):// is a Chrome --remote-debugging
    endpoint whose /json/version tells us the browser websocket (local dev on Trainer)."""
    if CDP_URL.startswith("ws"):
        return CDP_URL
    from urllib.request import urlopen
    with urlopen(CDP_URL.rstrip("/") + "/json/version", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))["webSocketDebuggerUrl"]


def screenshot_url(url, settle_ms=1200, timeout_s=25):
    """Raw CDP: open a target, size it 800x480, navigate, wait for load, capture PNG bytes."""
    import websocket
    ws = websocket.create_connection(_cdp_ws_url(), timeout=timeout_s, suppress_origin=True)
    ids = itertools.count(1)
    events = []

    def send(method, params=None, session=None):
        i = next(ids)
        msg = {"id": i, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        ws.send(json.dumps(msg))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == i:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})
            events.append(m)

    target = None
    try:
        target = send("Target.createTarget", {"url": "about:blank"})["targetId"]
        sess = send("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        send("Page.enable", session=sess)
        send("Emulation.setDeviceMetricsOverride",
             {"width": W, "height": H, "deviceScaleFactor": 1, "mobile": False}, session=sess)
        send("Page.navigate", {"url": url}, session=sess)
        deadline = _now() + timeout_s
        loaded = any(e.get("method") == "Page.loadEventFired" and e.get("sessionId") == sess for e in events)
        while not loaded and _now() < deadline:
            m = json.loads(ws.recv())
            loaded = m.get("method") == "Page.loadEventFired" and m.get("sessionId") == sess
        time.sleep(settle_ms / 1000)
        shot = send("Page.captureScreenshot",
                    {"format": "png", "clip": {"x": 0, "y": 0, "width": W, "height": H, "scale": 1}}, session=sess)
        return base64.b64decode(shot["data"])
    finally:
        try:
            if target:
                send("Target.closeTarget", {"targetId": target})
        except Exception:
            pass
        ws.close()


def to_1bit(png_bytes, fmt="png"):
    """800x480 1-bit, Floyd-Steinberg dithered. PNG for FW >= 1.5.2, BMP otherwise."""
    from PIL import Image
    im = Image.open(io.BytesIO(png_bytes)).convert("L")
    if im.size != (W, H):
        im = im.resize((W, H))
    bw = im.convert("1")
    buf = io.BytesIO()
    bw.save(buf, format="PNG" if fmt == "png" else "BMP")
    return buf.getvalue()


def text_image(lines, fmt="png"):
    """Pillow-only fallback screen (renderer down, unknown screen, setup splash)."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("1", (W, H), 1)
    d = ImageDraw.Draw(im)
    try:
        big = ImageFont.truetype("DejaVuSans-Bold.ttf", 34)
        small = ImageFont.truetype("DejaVuSans.ttf", 22)
    except OSError:
        big = small = ImageFont.load_default()
    y = 120
    for i, line in enumerate(lines[:6]):
        f = big if i == 0 else small
        w = d.textlength(line, font=f)
        d.text(((W - w) / 2, y), line, font=f, fill=0)
        y += 56 if i == 0 else 34
    d.rectangle([24, 24, W - 25, H - 25], outline=0, width=3)
    buf = io.BytesIO()
    im.save(buf, format="PNG" if fmt == "png" else "BMP")
    return buf.getvalue()


def render_screen(spec, fmt="png"):
    """spec: a SCREENS key, 'text:<msg>' or 'url:<http…>'. Returns (bytes, slug, error|None).
    Never raises — a failed render becomes a text screen so the wall is never blank."""
    key = spec if spec in SCREENS else spec.split(":", 1)[0]
    slug = re.sub(r"[^a-z0-9]+", "-", key.lower())[:24] or "screen"
    cached = _render_cache.get(spec)
    if cached and _now() - cached["at"] < RENDER_TTL_S:
        return to_1bit(cached["png"], fmt), slug, None
    try:
        if spec.startswith("url:"):
            url = spec[4:].strip()
        elif spec.startswith("text:"):
            from urllib.parse import quote
            url = f"{INTERNAL_URL}/byos/screen/text?msg={quote(spec[5:].strip())}"
        elif spec in SCREENS:
            url = f"{INTERNAL_URL}/byos/screen/{spec}"
        else:
            return text_image(["Unknown screen", spec, "wall: " + " | ".join(SCREENS)], fmt), slug, f"unknown screen {spec}"
        png = screenshot_url(url)
        _render_cache[spec] = {"at": _now(), "png": png}
        return to_1bit(png, fmt), slug, None
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:200]
        return text_image(["Nexus BYOS", f"render failed: {spec}", err, _iso()], fmt), slug, err


# ── policy ────────────────────────────────────────────────────────────────────
def _fw_tuple(fw):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", fw or "")[:3]) or (0,)
    except ValueError:
        return (0,)


def image_format_for(fw):
    return "png" if _fw_tuple(fw) >= (1, 5, 2) else "bmp"


def effective_mode(st, dev):
    if st.get("mode") in ("desk", "battery"):
        return st["mode"]
    volts = [v for v in (dev or {}).get("volts", [])[-DESK_N:] if isinstance(v, (int, float))]
    return "desk" if len(volts) == DESK_N and min(volts) >= DESK_VOLTS else "battery"


def active_wall(st):
    w = st.get("wall")
    if w and w.get("until", 0) > _now():
        return w
    return None


def current_screen(st):
    """(spec, is_pinned). Playlist position is derived from the clock so it needs no state
    and a screen never 'sticks' because a poll was missed."""
    w = active_wall(st)
    if w:
        return w["screen"], True
    pl = [s for s in st.get("playlist") or DEFAULT_PLAYLIST if s] or DEFAULT_PLAYLIST
    return pl[int(_now() // ROTATE_S) % len(pl)], False


def refresh_for(st, dev):
    mode = effective_mode(st, dev)
    w = active_wall(st)
    rr = REFRESH[mode]["wall" if w else "idle"]
    if w:
        rr = min(rr, max(MIN_REFRESH, int(w["until"] - _now()) + 5))
    return max(MIN_REFRESH, int(rr)), mode


def _prune_images():
    try:
        for fn in os.listdir(IMG_DIR):
            p = os.path.join(IMG_DIR, fn)
            if fn != "setup.png" and _now() - os.path.getmtime(p) > IMG_KEEP_S:
                os.remove(p)
    except OSError:
        pass


def _write_image(data, slug, fmt):
    os.makedirs(IMG_DIR, exist_ok=True)
    name = f"{slug}-{hashlib.sha1(data).hexdigest()[:10]}.{fmt}"
    p = os.path.join(IMG_DIR, name)
    if not os.path.exists(p):
        with open(p, "wb") as f:
            f.write(data)
    return name


# ── device surface ─────────────────────────────────────────────────────────────
def _device_by_token(st, token):
    for mac, d in st.get("devices", {}).items():
        if token and d.get("api_key") == token:
            return mac, d
    return None, None


@device_bp.route("/api/setup")
def api_setup():
    mac = (request.headers.get("ID") or "").strip().upper()
    if not mac:
        return jsonify({"status": 404, "message": "ID header (MAC) required"}), 200
    with _lock:
        st = load_state()
        allowed = os.environ.get("BYOS_ALLOWED_MACS", "")
        if allowed and mac not in [m.strip().upper() for m in allowed.split(",") if m.strip()]:
            _note_error(st, "setup", f"refused unknown device {mac}")
            save_state(st)
            return jsonify({"status": 404, "message": "device not allowed"}), 200
        dev = st["devices"].get(mac) or {"friendly_id": secrets.token_hex(3).upper(), "volts": []}
        if not dev.get("api_key"):
            dev["api_key"] = secrets.token_urlsafe(24)
        dev.update(fw=request.headers.get("FW-Version"), model=request.headers.get("Model"),
                   paired_at=dev.get("paired_at") or _iso(), last_seen=_iso())
        st["devices"][mac] = dev
        save_state(st)
    os.makedirs(IMG_DIR, exist_ok=True)
    setup_png = os.path.join(IMG_DIR, "setup.png")
    with open(setup_png, "wb") as f:
        f.write(text_image(["Nexus BYOS", f"paired · {dev['friendly_id']}", mac, "Tap the button for a screen"]))
    return jsonify({"status": 200, "api_key": dev["api_key"], "friendly_id": dev["friendly_id"],
                    "image_url": f"{BASE_URL}/byos/img/setup.png", "message": "Welcome to Nexus"})


@device_bp.route("/api/display")
def api_display():
    h = request.headers
    token = h.get("Access-Token")
    with _lock:
        st = load_state()
        mac, dev = _device_by_token(st, token)
        if dev is None:
            _note_error(st, "display", f"unknown token from ID={h.get('ID')}")
            save_state(st)
            return jsonify({"status": 500, "error": "Device not found — re-run setup", "reset_firmware": False}), 200
        try:
            v = float(h.get("Battery-Voltage") or "nan")
            if v == v:
                dev["volts"] = (dev.get("volts") or [])[-11:] + [round(v, 3)]
        except ValueError:
            pass
        dev.update(last_seen=_iso(), fw=h.get("FW-Version") or dev.get("fw"), rssi=h.get("RSSI"),
                   source=h.get("Update-Source"), last_refresh_rate=h.get("Refresh-Rate"),
                   width=h.get("Width"), height=h.get("Height"))
        if (h.get("special_function") or "").lower() == "true":
            dev["button_special_at"] = _iso()
        spec, pinned = current_screen(st)
        rr, mode = refresh_for(st, dev)
        fmt = image_format_for(dev.get("fw"))
        data, slug, err = render_screen(spec, fmt)
        if err:
            _note_error(st, f"render:{spec}", err)
        name = _write_image(data, slug, fmt)
        _prune_images()
        dev["last_filename"] = name
        st["last_display"] = {"at": _iso(), "screen": spec, "pinned": pinned, "mode": mode,
                              "refresh_rate": rr, "filename": name, "bytes": len(data), "error": err}
        save_state(st)
    return jsonify({"status": 0, "image_url": f"{BASE_URL}/byos/img/{name}", "image_url_timeout": 0,
                    "filename": name, "refresh_rate": rr, "update_firmware": False, "firmware_url": None,
                    "reset_firmware": False, "special_function": "none"})


@device_bp.route("/api/log", methods=["POST"])
def api_log():
    body = request.get_json(silent=True) or {}
    with _lock:
        st = load_state()
        st.setdefault("log", []).append({"at": _iso(), "id": request.headers.get("ID"), "body": body})
        st["log"] = st["log"][-50:]
        save_state(st)
    return "", 204


@device_bp.route("/byos/img/<path:fname>")
def byos_img(fname):
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.(png|bmp)", fname):
        abort(404)
    mt = "image/png" if fname.endswith(".png") else "image/bmp"
    resp = send_from_directory(IMG_DIR, fname, mimetype=mt, max_age=0)
    resp.headers["Content-Type"] = mt   # the firmware picks its decoder from this header
    return resp


# ── nexus (control) surface ────────────────────────────────────────────────────
def _allowed():
    # Same rule as app._is_nexus_allowed: straight over Tailscale/LAN, never via Cloudflare.
    return request.headers.get("CF-Connecting-IP") is None


@nexus_bp.route("/byos/screen/<name>")
def byos_screen(name):
    if not _allowed():
        abort(404)
    try:
        html = screen_html(name, text=request.args.get("msg"))
    except KeyError:
        abort(404)
    return Response(html, mimetype="text/html")


@nexus_bp.route("/byos/preview/<name>.png")
def byos_preview(name):
    if not _allowed():
        abort(404)
    spec = name if name in SCREENS else request.args.get("spec", name)
    data, _, err = render_screen(spec, "png")
    resp = Response(data, mimetype="image/png")
    if err:
        resp.headers["X-BYOS-Error"] = err
    return resp


def wall_status(st):
    w = active_wall(st)
    spec, pinned = current_screen(st)
    devs = []
    for mac, d in st.get("devices", {}).items():
        devs.append({"mac": mac, "friendly_id": d.get("friendly_id"), "fw": d.get("fw"), "last_seen": d.get("last_seen"),
                     "volts": (d.get("volts") or [None])[-1], "rssi": d.get("rssi"), "source": d.get("source"),
                     "mode": effective_mode(st, d)})
    return {"screen": spec, "pinned": pinned,
            "wall": {"screen": w["screen"], "until": _iso(w["until"]), "minutes_left": int((w["until"] - _now()) / 60) + 1,
                     "set_by": w.get("set_by")} if w else None,
            "mode": st.get("mode", "auto"), "playlist": st.get("playlist") or DEFAULT_PLAYLIST,
            "rotate_minutes": ROTATE_S // 60, "screens": {k: v[2] for k, v in SCREENS.items()},
            "devices": devs, "last_display": st.get("last_display"), "errors": (st.get("errors") or [])[-3:]}


def _one_liner(s):
    d = s["devices"][0] if s["devices"] else None
    seen = f"{d['friendly_id']} seen {d['last_seen'][11:16]} · {d['volts']} V · {d['mode']}" if d else "no device paired"
    if s["wall"]:
        return f"wall: {s['wall']['screen']} pinned {s['wall']['minutes_left']} min more · {seen}"
    return f"wall: playlist ({s['screen']} now, {s['rotate_minutes']}m rotation) · {seen}"


@nexus_bp.route("/api/wall", methods=["GET", "POST"])
def api_wall():
    """GET -> status. POST {"screen": "<name>|url:<…>|text:<…>", "minutes": 30} pins;
    {"screen":"clear"} unpins; {"mode":"desk|battery|auto"}; {"playlist":[...]}.
    Every reply carries `msg`, a one-liner Aernbot can relay verbatim."""
    if not _allowed():
        return jsonify({"status": "ok"})
    body = request.get_json(silent=True) or {} if request.method == "POST" else {}
    with _lock:
        st = load_state()
        msg = None
        if request.method == "POST":
            who = body.get("set_by") or "nexus"
            if body.get("mode"):
                m = str(body["mode"]).lower()
                if m not in ("desk", "battery", "auto"):
                    return jsonify({"ok": False, "error": "mode must be desk|battery|auto"}), 400
                st["mode"] = m
                msg = f"mode: {m}"
            if isinstance(body.get("playlist"), list):
                bad = [s for s in body["playlist"] if s not in SCREENS and not str(s).startswith(("url:", "text:"))]
                if bad or not body["playlist"]:
                    return jsonify({"ok": False, "error": f"unknown screens: {bad or 'empty'}"}), 400
                st["playlist"] = list(body["playlist"])
                msg = "playlist: " + ", ".join(st["playlist"])
            if body.get("screen"):
                spec = str(body["screen"]).strip()
                if spec.lower() in ("clear", "off", "none", "playlist"):
                    st["wall"] = None
                    msg = "wall cleared — back to the playlist"
                else:
                    if spec not in SCREENS and not spec.startswith(("url:", "text:")):
                        return jsonify({"ok": False, "error": f"unknown screen {spec!r}; "
                                        f"screens: {', '.join(SCREENS)} or url:<…> / text:<…>"}), 400
                    try:
                        mins = max(1, min(MAX_WALL_MIN, int(body.get("minutes") or 30)))
                    except (TypeError, ValueError):
                        mins = 30
                    st["wall"] = {"screen": spec, "until": _now() + mins * 60, "set_by": who, "at": _iso()}
                    _render_cache.pop(spec, None)
                    msg = f"wall: {spec} for {mins} min (device picks it up on its next wake — tap the button to force it)"
            if msg is None:
                return jsonify({"ok": False, "error": "screen / mode / playlist required"}), 400
            save_state(st)
        s = wall_status(st)
    s["ok"] = True
    s["msg"] = msg or _one_liner(s)
    return jsonify(s)


# ── standalone device app (LAN-bound listener started by app.py) ───────────────
def make_device_app():
    a = Flask("byos_device")
    a.register_blueprint(device_bp)
    return a


def start_device_listener(port=None):
    """Serve the device surface on its own port in a daemon thread (waitress)."""
    port = int(port or os.environ.get("BYOS_DEVICE_PORT", "5556"))
    from waitress import serve
    t = threading.Thread(target=lambda: serve(make_device_app(), host="0.0.0.0", port=port, threads=4),
                         name="byos-device", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    # Local dev: `python byos.py` serves BOTH surfaces on one port (5557) — point
    # BYOS_CDP_URL at a local headless Chrome/Edge (--remote-debugging-port) to test.
    app = Flask("byos_dev")
    app.register_blueprint(device_bp)
    app.register_blueprint(nexus_bp)
    from waitress import serve
    serve(app, host="127.0.0.1", port=int(os.environ.get("BYOS_DEV_PORT", "5557")), threads=4)
