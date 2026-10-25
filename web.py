#!/usr/bin/env python3
"""Webinterface für bambu-twitch: Einstellungen, Dienststeuerung, Log. Passwortgeschützt."""
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)
from jinja2 import DictLoader
from werkzeug.security import check_password_hash, generate_password_hash

CONF_DIR = Path(os.environ.get("BAMBU_CONF_DIR", "/etc/bambu-twitch"))
CONFIG_FILE = CONF_DIR / "config.env"
AUTH_FILE = CONF_DIR / "web.json"
SECRET_FILE = CONF_DIR / "web_secret"
SERVICE = os.environ.get("BAMBU_SERVICE", "bambu-twitch")
PORT = int(os.environ.get("BAMBU_WEB_PORT", "8080"))
PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"]

FIELDS = [
    ("Quellen (Bambuddy)", [
        dict(k="BAMBUDDY_CAMERA_URL", label="Kamera-URL", t="url", req=True, d="",
             help="MJPEG-Stream von Bambuddy, inkl. Token"),
        dict(k="BAMBUDDY_OVERLAY_URL", label="Overlay-URL", t="url", req=True, d="",
             help="Overlay-Seite von Bambuddy, inkl. Token"),
        dict(k="OVERLAY_CSS", label="Zusatz-CSS für das Overlay", t="text", d="",
             help="z. B. .min-h-screen.bg-black{background:transparent!important}"),
    ]),
    ("Ziele", [
        dict(k="TWITCH_ENABLED", label="Twitch aktiv", t="bool", d="1"),
        dict(k="TWITCH_STREAM_KEY", label="Twitch-Stream-Key", t="secret", d=""),
        dict(k="YOUTUBE_ENABLED", label="YouTube aktiv", t="bool", d="1"),
        dict(k="YOUTUBE_STREAM_KEY", label="YouTube-Streamschlüssel", t="secret", d=""),
    ]),
    ("Video", [
        dict(k="WIDTH", label="Breite", t="int", lo=320, hi=3840, d="1280"),
        dict(k="HEIGHT", label="Höhe", t="int", lo=180, hi=2160, d="720"),
        dict(k="FPS", label="Bilder pro Sekunde", t="int", lo=5, hi=60, d="15"),
        dict(k="OVERLAY_FPS", label="Overlay-Bilder pro Sekunde", t="int", lo=1, hi=30, d="5"),
        dict(k="VIDEO_BITRATE", label="Video-Bitrate", t="rate", d="3500k", help="z. B. 3500k, Twitch maximal 6000k"),
        dict(k="X264_PRESET", label="x264-Preset", t="select", d="veryfast",
             help="schneller = weniger CPU, schlechtere Qualität"),
        dict(k="SILENT_AUDIO", label="Stille Tonspur einmischen", t="bool", d="1",
             help="Twitch verlangt eine Tonspur"),
    ]),
    ("Erweitert", [
        dict(k="TWITCH_INGEST", label="Twitch-Ingest", t="rtmp", d="rtmp://live.twitch.tv/app"),
        dict(k="YOUTUBE_INGEST", label="YouTube-Ingest", t="rtmp", d="rtmp://a.rtmp.youtube.com/live2"),
        dict(k="RETRY_DELAY", label="Wartezeit vor Neustart (s)", t="int", lo=1, hi=300, d="10"),
    ]),
]
ALL = {f["k"]: f for _, fs in FIELDS for f in fs}

# ---------------------------------------------------------------- Config
def read_config() -> dict:
    cfg = {}
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                q, v = v[0], v[1:-1]
                if q == '"':
                    v = re.sub(r'\\(["\\])', r"\1", v)
            cfg[k.strip()] = v
    return cfg


def write_config(cfg: dict) -> None:
    def q(v):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    lines = ["# Von bambu-twitch-web verwaltet"] + [f"{k}={q(v)}" for k, v in cfg.items()]
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_FILE)


def validate(f, raw):
    """-> (wert, fehler)"""
    if f["t"] == "bool":
        return ("1" if raw else "0"), None
    raw = (raw or "").strip()
    if "\n" in raw or "\r" in raw:
        return raw, "Keine Zeilenumbrüche erlaubt"
    if not raw:
        return raw, ("Pflichtfeld" if f.get("req") else None)
    t = f["t"]
    if t == "url" and not re.match(r"^https?://\S+$", raw):
        return raw, "Muss mit http:// oder https:// beginnen"
    if t == "rtmp" and not re.match(r"^rtmps?://\S+$", raw):
        return raw, "Muss mit rtmp:// oder rtmps:// beginnen"
    if t == "rate" and not re.match(r"^\d{3,5}k$", raw):
        return raw, "Format wie 3500k"
    if t == "select" and raw not in PRESETS:
        return raw, "Ungültiges Preset"
    if t == "secret" and re.search(r"\s", raw):
        return raw, "Darf keine Leerzeichen enthalten"
    if t == "int":
        try:
            n = int(raw)
        except ValueError:
            return raw, "Ganze Zahl erwartet"
        if not f["lo"] <= n <= f["hi"]:
            return raw, f"Zwischen {f['lo']} und {f['hi']}"
    return raw, None

# ---------------------------------------------------------------- systemd / Log
def sc(*args):
    r = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=30)
    return r.stdout.strip(), r.returncode


def mask(text: str) -> str:
    text = re.sub(r"(token=)[^&\s\"']+", r"\1***", text)
    for k, v in read_config().items():
        if k.endswith("_KEY") and v:
            text = text.replace(v, "***")
    return text


def status() -> dict:
    state, _ = sc("is-active", SERVICE)
    enabled, _ = sc("is-enabled", SERVICE)
    since, _ = sc("show", SERVICE, "-p", "ActiveEnterTimestamp", "--value")
    log = subprocess.run(["journalctl", "-u", SERVICE, "-n", "60", "--no-pager"],
                         capture_output=True, text=True, timeout=15).stdout
    return dict(state=state, enabled=enabled, since=since, log=mask(log))

# ---------------------------------------------------------------- Auth
def make_password() -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(14))


def set_auth(pw: str, must_change: bool) -> None:
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps({"hash": generate_password_hash(pw), "must_change": must_change}))
    os.chmod(AUTH_FILE, 0o600)


def load_auth():
    try:
        return json.loads(AUTH_FILE.read_text())
    except (OSError, ValueError):
        return None


def load_secret() -> bytes:
    if not SECRET_FILE.exists():
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_text(secrets.token_hex(32))
        os.chmod(SECRET_FILE, 0o600)
    return SECRET_FILE.read_text().strip().encode()


FAILS: dict = {}  # ip -> (anzahl, gesperrt_bis)

# ---------------------------------------------------------------- App
app = Flask(__name__)
app.config.update(SECRET_KEY=load_secret(), SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Strict", PERMANENT_SESSION_LIFETIME=timedelta(hours=12))


def csrf() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


app.jinja_env.globals["csrf"] = csrf


@app.before_request
def guard():
    if request.method == "POST":
        tok = request.form.get("csrf", "")
        if not tok or not hmac.compare_digest(tok, session.get("csrf", "")):
            abort(400)
    if request.endpoint in ("login", "static"):
        return None
    if not session.get("auth"):
        if request.path.startswith("/api/"):
            return jsonify(error="unauthorized"), 401
        return redirect(url_for("login"))
    auth = load_auth()
    if auth and auth.get("must_change") and request.endpoint not in ("password", "logout"):
        return redirect(url_for("password"))
    return None


@app.after_request
def headers(r):
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["Cache-Control"] = "no-store"
    r.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
    return r


@app.route("/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr or "?"
    if request.method == "POST":
        n, until = FAILS.get(ip, (0, 0))
        if n >= 5 and time.time() < until:
            flash("Zu viele Fehlversuche. Bitte in ein paar Minuten erneut versuchen.", "err")
            return render_template("login.html"), 429
        auth = load_auth()
        if auth and check_password_hash(auth["hash"], request.form.get("password", "")):
            FAILS.pop(ip, None)
            session.clear()
            session.permanent = True
            session["auth"] = True
            return redirect(url_for("index"))
        n = 0 if n >= 5 and time.time() >= until else n
        FAILS[ip] = (n + 1, time.time() + 300)
        time.sleep(1)
        flash("Passwort falsch.", "err")
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
def password():
    auth = load_auth()
    if request.method == "POST":
        old, new, new2 = (request.form.get(x, "") for x in ("old", "new", "new2"))
        if not check_password_hash(auth["hash"], old):
            flash("Aktuelles Passwort falsch.", "err")
        elif len(new) < 10:
            flash("Das neue Passwort braucht mindestens 10 Zeichen.", "err")
        elif new != new2:
            flash("Die neuen Passwörter stimmen nicht überein.", "err")
        else:
            set_auth(new, False)
            flash("Passwort geändert.", "ok")
            return redirect(url_for("index"))
    return render_template("password.html", forced=bool(auth and auth.get("must_change")))


def render_index(values=None, errors=None, code=200):
    cfg = read_config()
    vals = {k: cfg.get(k, f["d"]) for k, f in ALL.items()}
    if values:
        vals.update(values)
    return render_template("index.html", groups=FIELDS, vals=vals, errors=errors or {},
                           presets=PRESETS, secret_set={k: bool(cfg.get(k)) for k in ALL}), code


@app.get("/")
def index():
    return render_index()


@app.get("/api/status")
def api_status():
    return jsonify(status())


@app.post("/service")
def service():
    a = request.form.get("a")
    if a not in ("start", "stop", "restart", "enable", "disable"):
        abort(400)
    _, rc = sc(a, SERVICE)
    flash(("Ausgeführt: " if rc == 0 else "Fehlgeschlagen: ") + a, "ok" if rc == 0 else "err")
    return redirect(url_for("index"))


@app.post("/save")
def save():
    cfg = read_config()
    new, errors, shown = dict(cfg), {}, {}
    for k, f in ALL.items():
        if f["t"] == "secret":
            typed = request.form.get(k, "").strip()
            if request.form.get("clear_" + k):
                new[k] = ""
            elif typed:
                v, err = validate(f, typed)
                if err:
                    errors[k] = err
                new[k] = v
            continue
        v, err = validate(f, request.form.get(k) if f["t"] != "bool" else request.form.get(k) == "1")
        new[k], shown[k] = v, v
        if err:
            errors[k] = err
    if errors:
        flash("Bitte die markierten Felder korrigieren, es wurde nichts gespeichert.", "err")
        return render_index(values=shown, errors=errors, code=400)
    write_config(new)
    if request.form.get("a") == "restart":
        _, rc = sc("restart", SERVICE)
        flash("Gespeichert, Stream neu gestartet." if rc == 0 else "Gespeichert, Neustart fehlgeschlagen.",
              "ok" if rc == 0 else "err")
    else:
        flash("Gespeichert. Änderungen gelten nach dem nächsten Neustart des Streams.", "ok")
    return redirect(url_for("index"))

# ---------------------------------------------------------------- Templates
CSS = """
:root{color-scheme:dark light;--bg:#14161a;--card:#1d2026;--fg:#e6e8eb;--mut:#8b93a1;--br:#2b303a;--ac:#4c8dff;--ok:#2ea36b;--er:#d9534f}
@media (prefers-color-scheme:light){:root{--bg:#f3f5f8;--card:#fff;--fg:#1c2128;--mut:#667085;--br:#d9dee6}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:16px}h1{font-size:20px;margin:0}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}
header form{display:flex;gap:12px;align-items:center}a{color:var(--ac)}
.card,fieldset{background:var(--card);border:1px solid var(--br);border-radius:10px;padding:14px;margin:0 0 16px}
legend{padding:0 6px;font-weight:600}label{display:block;margin:10px 0 2px;font-weight:500}
input[type=text],input[type=password],select{width:100%;padding:8px 10px;border-radius:8px;border:1px solid var(--br);background:var(--bg);color:var(--fg);font:inherit}
input.bad{border-color:var(--er)}.chk{display:flex;gap:8px;align-items:center;margin-top:12px}.chk label{margin:0}
small{display:block;color:var(--mut)}small.e{color:var(--er)}
button{padding:8px 14px;border-radius:8px;border:1px solid var(--br);background:var(--card);color:var(--fg);font:inherit;cursor:pointer}
button.primary{background:var(--ac);border-color:var(--ac);color:#fff}button.ghost{background:none}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}.muted{color:var(--mut)}
.dot{width:10px;height:10px;border-radius:50%;background:var(--mut)}.dot.active{background:var(--ok)}.dot.failed{background:var(--er)}
.msg{padding:10px 12px;border-radius:8px;margin-bottom:12px;border:1px solid var(--br)}.msg.ok{border-color:var(--ok)}.msg.err{border-color:var(--er)}
pre{margin:8px 0 0;max-height:260px;overflow:auto;background:var(--bg);border:1px solid var(--br);border-radius:8px;padding:8px;font-size:12px;white-space:pre-wrap;word-break:break-all}
.narrow{max-width:380px;margin-top:10vh}
"""

TEMPLATES = {
"base.html": """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Bambu → Stream</title>
<style>""" + CSS + """</style></head><body><main class="{% block cls %}{% endblock %}">
{% for c, t in get_flashed_messages(with_categories=true) %}<div class="msg {{ c }}">{{ t }}</div>{% endfor %}
{% block c %}{% endblock %}</main></body></html>""",

"login.html": """{% extends "base.html" %}{% block cls %}narrow{% endblock %}{% block c %}
<h1>Anmeldung</h1><form method="post" class="card" style="margin-top:12px">
<input type="hidden" name="csrf" value="{{ csrf() }}"><label for="p">Passwort</label>
<input id="p" type="password" name="password" autofocus autocomplete="current-password">
<p><button class="primary">Anmelden</button></p></form>{% endblock %}""",

"password.html": """{% extends "base.html" %}{% block cls %}narrow{% endblock %}{% block c %}
<h1>Passwort ändern</h1>{% if forced %}<p class="muted">Bitte vergib jetzt ein eigenes Passwort.</p>{% endif %}
<form method="post" class="card"><input type="hidden" name="csrf" value="{{ csrf() }}">
<label>Aktuelles Passwort</label><input type="password" name="old" autocomplete="current-password">
<label>Neues Passwort (mind. 10 Zeichen)</label><input type="password" name="new" autocomplete="new-password">
<label>Neues Passwort wiederholen</label><input type="password" name="new2" autocomplete="new-password">
<p><button class="primary">Speichern</button> {% if not forced %}<a href="/">Abbrechen</a>{% endif %}</p></form>
<form method="post" action="/logout"><input type="hidden" name="csrf" value="{{ csrf() }}"><button class="ghost">Abmelden</button></form>
{% endblock %}""",

"index.html": """{% extends "base.html" %}{% block c %}
<header><h1>Bambu → Twitch / YouTube</h1>
<form method="post" action="/logout"><input type="hidden" name="csrf" value="{{ csrf() }}">
<a href="/password">Passwort ändern</a><button class="ghost">Abmelden</button></form></header>
<section class="card"><div class="row"><span id="dot" class="dot"></span><b id="state">…</b>
<span id="since" class="muted"></span><span id="en" class="muted"></span></div>
<form method="post" action="/service" class="row"><input type="hidden" name="csrf" value="{{ csrf() }}">
<button name="a" value="start">Start</button><button name="a" value="stop">Stop</button>
<button name="a" value="restart">Neustart</button><button name="a" value="enable">Autostart an</button>
<button name="a" value="disable">Autostart aus</button></form><pre id="log"></pre></section>
<form method="post" action="/save"><input type="hidden" name="csrf" value="{{ csrf() }}">
{% for title, fields in groups %}<fieldset><legend>{{ title }}</legend>
{% for f in fields %}{% set k = f.k %}
{% if f.t == "bool" %}<div class="chk"><input type="checkbox" id="{{ k }}" name="{{ k }}" value="1" {{ "checked" if vals[k] == "1" }}>
<label for="{{ k }}">{{ f.label }}</label></div>
{% elif f.t == "select" %}<label for="{{ k }}">{{ f.label }}</label><select id="{{ k }}" name="{{ k }}">
{% for p in presets %}<option {{ "selected" if vals[k] == p }}>{{ p }}</option>{% endfor %}</select>
{% elif f.t == "secret" %}<label for="{{ k }}">{{ f.label }}</label>
<input type="password" id="{{ k }}" name="{{ k }}" autocomplete="off" class="{{ 'bad' if errors[k] }}"
 placeholder="{{ 'gesetzt – leer lassen zum Behalten' if secret_set[k] else 'nicht gesetzt' }}">
{% if secret_set[k] %}<div class="chk"><input type="checkbox" id="c_{{ k }}" name="clear_{{ k }}" value="1">
<label for="c_{{ k }}">Schlüssel entfernen</label></div>{% endif %}
{% else %}<label for="{{ k }}">{{ f.label }}</label>
<input type="text" id="{{ k }}" name="{{ k }}" value="{{ vals[k] }}" class="{{ 'bad' if errors[k] }}" autocomplete="off" spellcheck="false">
{% endif %}
{% if errors[k] %}<small class="e">{{ errors[k] }}</small>{% elif f.help %}<small>{{ f.help }}</small>{% endif %}
{% endfor %}</fieldset>{% endfor %}
<div class="row"><button name="a" value="save">Speichern</button>
<button name="a" value="restart" class="primary">Speichern &amp; neu starten</button></div></form>
<script>
const $=id=>document.getElementById(id);
async function poll(){try{const r=await fetch('/api/status');if(r.status===401){location='/login';return}
const d=await r.json();$('state').textContent=d.state;$('dot').className='dot '+d.state;
$('since').textContent=d.since?'seit '+d.since:'';$('en').textContent='Autostart: '+d.enabled;
const l=$('log'),end=l.scrollTop+l.clientHeight>=l.scrollHeight-20;l.textContent=d.log;if(end)l.scrollTop=l.scrollHeight}catch(e){}}
poll();setInterval(poll,3000);</script>{% endblock %}""",
}
app.jinja_loader = DictLoader(TEMPLATES)

# ---------------------------------------------------------------- Start / CLI
if __name__ == "__main__":
    if "--init" in sys.argv:            # Initialpasswort nur erzeugen, wenn noch keins existiert
        if not AUTH_FILE.exists():
            pw = make_password()
            set_auth(pw, True)
            print(pw)
        sys.exit(0)
    if "--reset-password" in sys.argv:  # neues Initialpasswort setzen
        pw = make_password()
        set_auth(pw, True)
        print(pw)
        sys.exit(0)
    from waitress import serve
    print(f"Webinterface auf Port {PORT}", flush=True)
    serve(app, host="0.0.0.0", port=PORT, threads=4)
