import os, re, time, json, sqlite3, threading, secrets, html, logging
from datetime import datetime
import requests
import telebot
from flask import (Flask, request, session, redirect, url_for,
                   render_template_string, flash, jsonify, abort)

try:
    import urllib3
    urllib3.disable_warnings()
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("resetbot")

# ------------------------------------------------------------------ config
BOT_TOKEN = os.getenv("BOT_TOKEN", "8586614111:AAGMfrWdT_u_tzjSsGbHmHlSAcNHyvJs5ak")
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", "6739795427"))}
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "Reset@6739")
SECRET_KEY = os.getenv("SECRET_KEY", "k9f2c7a1e84b3d6059a7c21f8e4b6d03")
DB_PATH = os.getenv("DB_PATH", "bot.db")
PORT = int(os.getenv("PORT", "10000"))
BRAND = os.getenv("BRAND", "DRIP CLIENT RESET SYSTEM")
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
except Exception:
    TZ = None

TIERS = ["standard", "basic", "premium", "reseller"]
TIER_LABEL = {"standard": "📊 Standard", "basic": "💎 Basic",
              "premium": "⭐ Premium", "reseller": "🏷️ Reseller"}
DEFAULTS = {
    "api_base_url": os.getenv("API_BASE_URL", ""),
    "api_token": os.getenv("API_TOKEN", ""),
    "verify_ssl": "0",
    "limit_standard": "2",
    "limit_basic": "10",
    "limit_premium": "-1",
    "limit_reseller": "50",
    "api_status": "unknown",
    "api_info": "",
    "api_checked": "0",
}

# ------------------------------------------------------------------ database
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_lock = threading.RLock()


def q(sql, args=(), one=False, commit=False):
    with _lock:
        cur = _db.execute(sql, args)
        if commit:
            _db.commit()
        rows = cur.fetchone() if one else cur.fetchall()
        return rows


def init_db():
    with _lock:
        _db.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            tier TEXT DEFAULT 'standard', custom_limit INTEGER,
            banned INTEGER DEFAULT 0, joined_at INTEGER, last_seen INTEGER);
        CREATE TABLE IF NOT EXISTS resets(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            license_key TEXT, status TEXT, message TEXT, ts INTEGER);
        CREATE INDEX IF NOT EXISTS ix_resets_user_ts ON resets(user_id, ts);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        """)
        _db.commit()


def get_setting(k):
    r = q("SELECT value FROM settings WHERE key=?", (k,), one=True)
    if r is not None and r["value"] is not None:
        return r["value"]
    return DEFAULTS.get(k, "")


def set_setting(k, v):
    q("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (k, str(v)), commit=True)


def now():
    return int(time.time())


def fmt_ts(ts):
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(int(ts), TZ) if TZ else datetime.fromtimestamp(int(ts))
    return dt.strftime("%d %b %Y, %I:%M %p")


def fmt_left(sec):
    sec = max(0, int(sec))
    h, m = sec // 3600, (sec % 3600) // 60
    return f"{h}h {m}m" if h else f"{m}m"


# ------------------------------------------------------------------ users / limits
def upsert_user(u):
    t = now()
    q("INSERT OR IGNORE INTO users(id,username,first_name,joined_at,last_seen) VALUES(?,?,?,?,?)",
      (u.id, u.username or "", u.first_name or "", t, t), commit=True)
    q("UPDATE users SET username=?, first_name=?, last_seen=? WHERE id=?",
      (u.username or "", u.first_name or "", t, u.id), commit=True)
    return q("SELECT * FROM users WHERE id=?", (u.id,), one=True)


def tier_limit(tier):
    try:
        return int(get_setting("limit_" + tier))
    except ValueError:
        return 2


def user_limit(row):
    if row["custom_limit"] is not None:
        return int(row["custom_limit"])
    return tier_limit(row["tier"] if row["tier"] in TIERS else "standard")


def usage(uid):
    since = now() - 86400
    rows = q("SELECT ts FROM resets WHERE user_id=? AND status='success' AND ts>? ORDER BY ts",
             (uid, since))
    used = len(rows)
    next_free = (rows[0]["ts"] + 86400 - now()) if rows else 0
    return used, next_free


def lim_txt(n):
    return "Unlimited" if n < 0 else f"{n:,}"


# ------------------------------------------------------------------ reset API
def api_cfg():
    base = (get_setting("api_base_url") or "").strip().rstrip("/")
    token = (get_setting("api_token") or "").strip()
    verify = get_setting("verify_ssl") == "1"
    return base, token, verify


def call_reset(key):
    """returns (status, message) status in success|invalid|failed|busy|api_error|not_configured"""
    base, token, verify = api_cfg()
    if not base or not token:
        return "not_configured", "API not configured"
    try:
        r = requests.post(base + "/api/seller/reset-hwid", json={"license_key": key},
                          headers={"Authorization": "Bearer " + token,
                                   "Content-Type": "application/json"},
                          timeout=25, verify=verify)
    except requests.RequestException as e:
        log.warning("reset request error: %s", e)
        return "busy", "Could not reach the reset server"
    try:
        data = r.json()
    except Exception:
        data = {}
    if r.status_code == 200 and data.get("success"):
        return "success", str(data.get("message") or "Device reset")
    code = str(data.get("error") or "").upper()
    msg = str(data.get("message") or f"HTTP {r.status_code}")
    if code in ("MISSING_API_KEY", "INVALID_API_KEY", "KEY_REVOKED", "ACCOUNT_DISABLED"):
        return "api_error", f"{code}: {msg}"
    if r.status_code == 429 or code == "RATE_LIMITED":
        return "busy", msg
    low = (code + " " + msg).lower()
    if r.status_code == 404 or any(x in low for x in ("not found", "not_found", "invalid_key",
                                                       "invalid key", "invalid_license", "no such")):
        return "invalid", msg
    return "failed", msg


def test_api():
    base, token, verify = api_cfg()
    if not base or not token:
        set_setting("api_status", "not_configured")
        set_setting("api_info", "")
        set_setting("api_checked", now())
        return False, "Base URL and token are required"
    try:
        r = requests.get(base + "/api/seller/me", headers={"Authorization": "Bearer " + token},
                         timeout=20, verify=verify)
        data = r.json() if r.content else {}
    except Exception as e:
        set_setting("api_status", "failed")
        set_setting("api_info", str(e)[:200])
        set_setting("api_checked", now())
        return False, f"Connection failed: {e}"
    ok = r.status_code == 200 and data.get("success", True) is not False
    info = ""
    if ok:
        bal = None
        for src in (data, data.get("credits") if isinstance(data.get("credits"), dict) else {},
                    data.get("seller") if isinstance(data.get("seller"), dict) else {}):
            if isinstance(src, dict) and "balance" in src:
                bal = src["balance"]
                break
        if bal is None and isinstance(data.get("credits"), (int, float)):
            bal = data["credits"]
        info = f"Balance: {bal}" if bal is not None else "OK"
    else:
        info = f"{data.get('error', r.status_code)}: {data.get('message', '')}"[:200]
    set_setting("api_status", "connected" if ok else "failed")
    set_setting("api_info", info)
    set_setting("api_checked", now())
    return ok, info


# ------------------------------------------------------------------ telegram bot
bot = telebot.TeleBot(BOT_TOKEN or "0:placeholder", parse_mode="HTML", threaded=True, num_threads=8)
LINE = "━━━━━━━━━━━━━━━━━━━━━━"
KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{6,80}$")
_inflight = set()
_inflight_lock = threading.Lock()
esc = html.escape


def tiers_block():
    return (f"📊 <b>Access Tiers:</b>\n"
            f"├ 📊 Standard: {lim_txt(tier_limit('standard'))} resets/day\n"
            f"├ 💎 Basic: {lim_txt(tier_limit('basic'))} resets/day\n"
            f"├ ⭐ Premium: {lim_txt(tier_limit('premium'))}\n"
            f"└ 🏷️ Reseller: {lim_txt(tier_limit('reseller'))} resets/day")


def start_text(row):
    lim = user_limit(row)
    used, _ = usage(row["id"])
    status = "🔴 Banned" if row["banned"] else "🟢 Active"
    custom = " (custom)" if row["custom_limit"] is not None else ""
    return (f"{LINE}\n🚀 <b>{esc(BRAND)}</b>\n{LINE}\n\n"
            f"✅ <b>System Ready</b>\n"
            f"├ Your ID: <code>{row['id']}</code>\n"
            f"├ Tier: {TIER_LABEL.get(row['tier'], row['tier'])}\n"
            f"├ Your Limit: <b>{lim_txt(lim)}</b> resets/day{custom}\n"
            f"├ Used (24h): {used}\n"
            f"└ Status: {status}\n\n"
            f"📖 <b>How to Use:</b>\n"
            f"├ 1️⃣ Paste your Key For Reset\n"
            f"├ 2️⃣ Bot processes instantly\n"
            f"└ 3️⃣ Check status anytime\n\n"
            f"{tiers_block()}\n\n"
            f"⚡ <b>Commands:</b>\n"
            f"├ /status — Check usage\n"
            f"└ /help — Instructions\n{LINE}")


@bot.message_handler(commands=["start"])
def h_start(m):
    row = upsert_user(m.from_user)
    bot.send_message(m.chat.id, start_text(row))


@bot.message_handler(commands=["help"])
def h_help(m):
    upsert_user(m.from_user)
    bot.send_message(m.chat.id,
                     f"{LINE}\n📖 <b>HELP</b>\n{LINE}\n\n"
                     "1️⃣ Copy your license key\n"
                     "2️⃣ Paste it here and send\n"
                     "3️⃣ Wait a moment for the result\n\n"
                     "<b>Results:</b>\n"
                     "✅ Reset Successful\n❌ Invalid Key\n⛔ Limit Reached\n⚠️ Reset Not Successful\n\n"
                     "Limits use a rolling 24 hours — failed resets are <b>not</b> counted.\n"
                     "Use /status to see your usage.")


@bot.message_handler(commands=["status"])
def h_status(m):
    row = upsert_user(m.from_user)
    lim = user_limit(row)
    used, nxt = usage(row["id"])
    rem = "Unlimited" if lim < 0 else str(max(0, lim - used))
    extra = ""
    if lim >= 0 and used >= lim and nxt:
        extra = f"\n⏳ Next slot in: <b>{fmt_left(nxt)}</b>"
    bot.send_message(m.chat.id,
                     f"{LINE}\n📊 <b>YOUR STATUS</b>\n{LINE}\n\n"
                     f"├ ID: <code>{row['id']}</code>\n"
                     f"├ Tier: {TIER_LABEL.get(row['tier'], row['tier'])}\n"
                     f"├ Daily Limit: <b>{lim_txt(lim)}</b>\n"
                     f"├ Used (24h): <b>{used}</b>\n"
                     f"├ Remaining: <b>{rem}</b>\n"
                     f"└ Account: {'🔴 Banned' if row['banned'] else '🟢 Active'}{extra}")


# ---- admin commands in Telegram
def is_admin(m):
    return m.from_user.id in ADMIN_IDS


@bot.message_handler(commands=["admin"])
def h_admin(m):
    if not is_admin(m):
        return
    t = now() - 86400
    total = q("SELECT COUNT(*) c FROM users", one=True)["c"]
    ok = q("SELECT COUNT(*) c FROM resets WHERE status='success' AND ts>?", (t,), one=True)["c"]
    bad = q("SELECT COUNT(*) c FROM resets WHERE status!='success' AND ts>?", (t,), one=True)["c"]
    st = get_setting("api_status")
    ico = {"connected": "🟢 Connected", "failed": "🔴 Failed"}.get(st, "⚪ Not connected")
    bot.send_message(m.chat.id,
                     f"🛠 <b>ADMIN</b>\n├ Users: {total}\n├ Resets 24h: ✅ {ok} / ❌ {bad}\n"
                     f"└ API: {ico}\n\n"
                     "/tier &lt;id&gt; &lt;standard|basic|premium|reseller&gt;\n"
                     "/limit &lt;id&gt; &lt;number|-1|default&gt;\n/ban &lt;id&gt;  /unban &lt;id&gt;\n"
                     "Full panel: open your Render URL /login")


def _uid_arg(m):
    p = m.text.split()
    if len(p) < 2 or not p[1].lstrip("-").isdigit():
        return None, p
    return int(p[1]), p


@bot.message_handler(commands=["tier", "limit", "ban", "unban"])
def h_admin_cmds(m):
    if not is_admin(m):
        return
    cmd = m.text.split()[0].lstrip("/").split("@")[0]
    uid, p = _uid_arg(m)
    if uid is None:
        return bot.reply_to(m, "Usage: /" + cmd + " &lt;user_id&gt; ...")
    q("INSERT OR IGNORE INTO users(id,joined_at,last_seen) VALUES(?,?,?)", (uid, now(), 0), commit=True)
    if cmd == "tier":
        if len(p) < 3 or p[2] not in TIERS:
            return bot.reply_to(m, "Tier must be: " + ", ".join(TIERS))
        q("UPDATE users SET tier=? WHERE id=?", (p[2], uid), commit=True)
    elif cmd == "limit":
        if len(p) < 3:
            return bot.reply_to(m, "Usage: /limit &lt;id&gt; &lt;number|-1|default&gt;")
        if p[2].lower() == "default":
            q("UPDATE users SET custom_limit=NULL WHERE id=?", (uid,), commit=True)
        elif p[2].lstrip("-").isdigit():
            q("UPDATE users SET custom_limit=? WHERE id=?", (int(p[2]), uid), commit=True)
        else:
            return bot.reply_to(m, "Limit must be a number, -1, or default")
    elif cmd == "ban":
        q("UPDATE users SET banned=1 WHERE id=?", (uid,), commit=True)
    else:
        q("UPDATE users SET banned=0 WHERE id=?", (uid,), commit=True)
    bot.reply_to(m, "✅ Updated user <code>%d</code>" % uid)


# ---- key handler
@bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith("/"), content_types=["text"])
def h_key(m):
    row = upsert_user(m.from_user)
    uid = row["id"]
    if row["banned"]:
        return bot.reply_to(m, "🚫 <b>ACCESS DENIED</b>\nYour account is blocked. Contact admin.")
    key = m.text.strip()
    if not KEY_RE.match(key):
        return bot.reply_to(m, f"{LINE}\n❌ <b>INVALID KEY</b>\n{LINE}\n\n"
                               "That doesn't look like a valid key.\nPlease paste only your key.")
    lim = user_limit(row)
    used, nxt = usage(uid)
    if lim >= 0 and used >= lim:
        return bot.reply_to(m, f"{LINE}\n⛔ <b>LIMIT REACHED</b>\n{LINE}\n\n"
                               f"├ Used: {used}/{lim_txt(lim)}\n"
                               f"└ Next slot in: <b>{fmt_left(nxt)}</b>\n\n"
                               "Contact admin for a higher limit.")
    with _inflight_lock:
        if uid in _inflight:
            return bot.reply_to(m, "⏳ Please wait, your previous request is still processing.")
        _inflight.add(uid)
    try:
        wait = bot.reply_to(m, "⏳ <b>Processing...</b>")
        status, msg = call_reset(key)
        q("INSERT INTO resets(user_id,license_key,status,message,ts) VALUES(?,?,?,?,?)",
          (uid, key, status, msg[:300], now()), commit=True)
        if status == "success":
            used += 1
            left = "Unlimited" if lim < 0 else str(max(0, lim - used))
            text = (f"{LINE}\n✅ <b>RESET SUCCESSFUL</b>\n{LINE}\n\n"
                    f"├ 🔑 Key: <code>{esc(key)}</code>\n"
                    f"├ Used (24h): {used}/{lim_txt(lim)}\n"
                    f"└ Remaining: <b>{left}</b>\n\n"
                    "You can now log in on a new device.")
        elif status == "invalid":
            text = (f"{LINE}\n❌ <b>INVALID KEY</b>\n{LINE}\n\n"
                    f"🔑 <code>{esc(key)}</code>\nKey was not found. Check and try again.\n"
                    "<i>This attempt was not counted.</i>")
        elif status == "busy":
            text = (f"{LINE}\n⏳ <b>SERVER BUSY</b>\n{LINE}\n\n"
                    "Reset server is busy right now. Try again in a minute.\n"
                    "<i>This attempt was not counted.</i>")
        elif status in ("api_error", "not_configured"):
            text = (f"{LINE}\n🔧 <b>SERVICE UNAVAILABLE</b>\n{LINE}\n\n"
                    "Reset service is temporarily offline. Please contact admin.")
            for a in ADMIN_IDS:
                try:
                    bot.send_message(a, f"⚠️ Reset API problem: <code>{esc(msg)}</code>")
                except Exception:
                    pass
        else:
            text = (f"{LINE}\n⚠️ <b>RESET NOT SUCCESSFUL</b>\n{LINE}\n\n"
                    f"🔑 <code>{esc(key)}</code>\n"
                    f"Reason: {esc(msg)}\n<i>This attempt was not counted.</i>")
        try:
            bot.edit_message_text(text, m.chat.id, wait.message_id)
        except Exception:
            bot.send_message(m.chat.id, text)
    finally:
        with _inflight_lock:
            _inflight.discard(uid)


# ------------------------------------------------------------------ web admin panel
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.jinja_env.filters["ts"] = fmt_ts

BASE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{title}} · Reset Admin</title>
<style>
:root{--bg:#0b0d10;--card:#12161b;--bd:#222a33;--tx:#e6edf3;--mu:#8b98a5;--g:#22e07a;--r:#ff5d5d;--y:#ffc857}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
.top{display:flex;gap:6px;flex-wrap:wrap;align-items:center;padding:12px 16px;border-bottom:1px solid var(--bd);background:var(--card);position:sticky;top:0}
.top b{margin-right:10px;color:var(--g)}.top a{color:var(--mu);text-decoration:none;padding:6px 10px;border-radius:8px}
.top a:hover,.top a.on{background:#1b222a;color:var(--tx)}.sp{flex:1}
.wrap{max-width:1100px;margin:0 auto;padding:16px}.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px;margin-bottom:16px}
h2{margin:0 0 12px;font-size:16px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.stat{background:#0e1216;border:1px solid var(--bd);border-radius:10px;padding:12px}.stat small{color:var(--mu);display:block}.stat b{font-size:22px}
table{width:100%;border-collapse:collapse}th,td{padding:8px;border-bottom:1px solid var(--bd);text-align:left;vertical-align:middle}th{color:var(--mu);font-weight:500;font-size:12px}
input,select{background:#0b0f13;border:1px solid var(--bd);color:var(--tx);padding:7px 9px;border-radius:8px;font:inherit}input[type=text],input[type=password],input[type=number]{width:100%}
.s input,.s select{width:auto}.btn{background:var(--g);color:#04210f;border:0;padding:8px 14px;border-radius:8px;font-weight:600;cursor:pointer}
.btn.alt{background:#1f2730;color:var(--tx)}.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:12px}
label{color:var(--mu);font-size:12px;display:block;margin-bottom:4px}.ok{color:var(--g)}.bad{color:var(--r)}.warn{color:var(--y)}
.flash{padding:10px 14px;border-radius:8px;margin-bottom:12px;background:#10261a;border:1px solid #1d5a38}.flash.e{background:#2a1313;border-color:#6b2222}
.pill{padding:2px 8px;border-radius:99px;font-size:12px;background:#1b222a}.scroll{overflow-x:auto}code{color:var(--y)}
</style></head><body>
{% if session.get('admin') %}<div class="top"><b>⚡ RESET ADMIN</b>
<a href="/" class="{{'on' if tab=='d'}}">Dashboard</a><a href="/users" class="{{'on' if tab=='u'}}">Users</a>
<a href="/api" class="{{'on' if tab=='a'}}">API Settings</a><a href="/limits" class="{{'on' if tab=='l'}}">Limits</a>
<a href="/logs" class="{{'on' if tab=='g'}}">Logs</a><span class="sp"></span><a href="/logout">Logout</a></div>{% endif %}
<div class="wrap">{% with msgs=get_flashed_messages(with_categories=true) %}{% for c,m in msgs %}
<div class="flash {{'e' if c=='error'}}">{{m}}</div>{% endfor %}{% endwith %}{{body|safe}}</div></body></html>"""


def page(title, tab, body, **ctx):
    inner = render_template_string(body, **ctx)
    return render_template_string(BASE, title=title, tab=tab, body=inner)


def csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf"] = csrf


@app.before_request
def guard():
    if request.endpoint in ("health", "login", "static"):
        if request.endpoint == "login" and request.method == "POST":
            pass
        return
    if not session.get("admin"):
        return redirect(url_for("login"))
    if request.method == "POST" and request.form.get("_c") != session.get("csrf"):
        abort(400)


@app.route("/health")
def health():
    return "ok"


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if (secrets.compare_digest(request.form.get("u", ""), ADMIN_USER) and
                secrets.compare_digest(request.form.get("p", ""), ADMIN_PASS)):
            session["admin"] = True
            return redirect("/")
        flash("Wrong username or password", "error")
    body = """<div class="card" style="max-width:360px;margin:60px auto"><h2>🔐 Admin Login</h2>
    <form method="post"><label>Username</label><input type="text" name="u" autofocus><br><br>
    <label>Password</label><input type="password" name="p"><br><br>
    <button class="btn">Login</button></form></div>"""
    return page("Login", "", body)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
def dashboard():
    t = now() - 86400
    c = lambda sql, a=(): q(sql, a, one=True)["c"]
    st = dict(
        total=c("SELECT COUNT(*) c FROM users"),
        new=c("SELECT COUNT(*) c FROM users WHERE joined_at>?", (t,)),
        active=c("SELECT COUNT(*) c FROM users WHERE last_seen>?", (t,)),
        ok=c("SELECT COUNT(*) c FROM resets WHERE status='success' AND ts>?", (t,)),
        bad=c("SELECT COUNT(*) c FROM resets WHERE status!='success' AND ts>?", (t,)),
        allok=c("SELECT COUNT(*) c FROM resets WHERE status='success'"),
        banned=c("SELECT COUNT(*) c FROM users WHERE banned=1"),
    )
    tiers = {x: c("SELECT COUNT(*) c FROM users WHERE tier=?", (x,)) for x in TIERS}
    logs = q("SELECT r.*, u.username FROM resets r LEFT JOIN users u ON u.id=r.user_id ORDER BY r.id DESC LIMIT 10")
    api = dict(status=get_setting("api_status"), info=get_setting("api_info"),
               checked=int(get_setting("api_checked") or 0))
    body = """
    <div class="card"><h2>API Status</h2>
      {% if api.status=='connected' %}<b class="ok">🟢 Connected</b> <span class="pill">{{api.info}}</span>
      {% elif api.status=='failed' %}<b class="bad">🔴 Connection failed</b> <span class="pill">{{api.info}}</span>
      {% else %}<b class="warn">⚪ Not connected</b> — set it in <a href="/api" style="color:var(--g)">API Settings</a>{% endif %}
      <div style="color:var(--mu);margin-top:6px">Last checked: {{api.checked|ts}}</div></div>
    <div class="grid">
      <div class="stat"><small>Total users</small><b>{{st.total}}</b></div>
      <div class="stat"><small>New (24h)</small><b>{{st.new}}</b></div>
      <div class="stat"><small>Active (24h)</small><b>{{st.active}}</b></div>
      <div class="stat"><small>Resets ✅ (24h)</small><b class="ok">{{st.ok}}</b></div>
      <div class="stat"><small>Failed ❌ (24h)</small><b class="bad">{{st.bad}}</b></div>
      <div class="stat"><small>Total resets</small><b>{{st.allok}}</b></div>
      <div class="stat"><small>Banned</small><b>{{st.banned}}</b></div></div><br>
    <div class="card"><h2>Users by tier</h2>{% for k,v in tiers.items() %}<span class="pill">{{k}}: {{v}}</span> {% endfor %}</div>
    <div class="card"><h2>Recent activity</h2><div class="scroll"><table><tr><th>Time</th><th>User</th><th>Key</th><th>Status</th><th>Message</th></tr>
    {% for r in logs %}<tr><td>{{r.ts|ts}}</td><td>{{r.user_id}} {{('@'+r.username) if r.username}}</td><td><code>{{r.license_key}}</code></td>
    <td class="{{'ok' if r.status=='success' else 'bad'}}">{{r.status}}</td><td>{{r.message}}</td></tr>{% endfor %}</table></div></div>"""
    return page("Dashboard", "d", body, st=st, tiers=tiers, logs=logs, api=api)


@app.route("/users")
def users():
    s = request.args.get("q", "").strip()
    pg = max(1, int(request.args.get("p", "1") or 1))
    where, args = "", []
    if s:
        where = "WHERE CAST(id AS TEXT) LIKE ? OR username LIKE ? OR first_name LIKE ?"
        args = [f"%{s}%"] * 3
    total = q(f"SELECT COUNT(*) c FROM users {where}", args, one=True)["c"]
    rows = q(f"SELECT * FROM users {where} ORDER BY last_seen DESC LIMIT 50 OFFSET ?", args + [(pg - 1) * 50])
    data = []
    for r in rows:
        used, _ = usage(r["id"])
        data.append(dict(r=r, used=used, lim=user_limit(r)))
    body = """
    <div class="card"><h2>➕ Add / update user by Telegram ID</h2>
    <form method="post" action="/users/add" class="s"><input type="hidden" name="_c" value="{{csrf()}}">
    <input type="number" name="id" placeholder="Telegram ID" required style="width:170px">
    <select name="tier">{% for t in tiers %}<option>{{t}}</option>{% endfor %}</select>
    <input type="number" name="custom" placeholder="Custom limit (e.g. 1000, -1=∞)" style="width:230px">
    <button class="btn">Save</button></form></div>
    <div class="card"><h2>Users ({{total}})</h2>
    <form class="s" style="margin-bottom:10px"><input name="q" value="{{s}}" placeholder="Search ID / username / name">
    <button class="btn alt">Search</button></form>
    <div class="scroll"><table><tr><th>User</th><th>Used 24h</th><th>Limit</th><th>Tier</th><th>Custom limit</th><th>Ban</th><th></th></tr>
    {% for d in data %}<tr>
    <td><form id="f{{d.r.id}}" method="post" action="/users/update"><input type="hidden" name="_c" value="{{csrf()}}"><input type="hidden" name="id" value="{{d.r.id}}"></form>
    <b>{{d.r.first_name or '—'}}</b> {{('@'+d.r.username) if d.r.username}}<br><small style="color:var(--mu)">{{d.r.id}} · seen {{d.r.last_seen|ts}}</small></td>
    <td>{{d.used}}</td><td>{{'∞' if d.lim<0 else d.lim}}</td>
    <td><select form="f{{d.r.id}}" name="tier">{% for t in tiers %}<option {{'selected' if t==d.r.tier}}>{{t}}</option>{% endfor %}</select></td>
    <td><input form="f{{d.r.id}}" type="number" name="custom" value="{{'' if d.r.custom_limit is none else d.r.custom_limit}}" placeholder="default" style="width:110px"></td>
    <td><input form="f{{d.r.id}}" type="checkbox" name="banned" value="1" {{'checked' if d.r.banned}}></td>
    <td><button form="f{{d.r.id}}" class="btn">Save</button></td></tr>{% endfor %}</table></div>
    <div style="margin-top:10px">{% if pg>1 %}<a href="?q={{s}}&p={{pg-1}}" style="color:var(--g)">← Prev</a>{% endif %}
    {% if pg*50<total %} <a href="?q={{s}}&p={{pg+1}}" style="color:var(--g)">Next →</a>{% endif %}</div></div>"""
    return page("Users", "u", body, data=data, total=total, s=s, pg=pg, tiers=TIERS)


def _custom(v):
    v = (v or "").strip()
    if v == "":
        return None
    try:
        return int(v)
    except ValueError:
        return None


@app.route("/users/add", methods=["POST"])
def users_add():
    try:
        uid = int(request.form["id"])
    except Exception:
        flash("Invalid Telegram ID", "error")
        return redirect("/users")
    tier = request.form.get("tier", "standard")
    tier = tier if tier in TIERS else "standard"
    q("INSERT OR IGNORE INTO users(id,joined_at,last_seen) VALUES(?,?,0)", (uid, now()), commit=True)
    q("UPDATE users SET tier=?, custom_limit=? WHERE id=?", (tier, _custom(request.form.get("custom")), uid), commit=True)
    flash(f"User {uid} saved", "ok")
    return redirect("/users")


@app.route("/users/update", methods=["POST"])
def users_update():
    uid = int(request.form["id"])
    tier = request.form.get("tier", "standard")
    tier = tier if tier in TIERS else "standard"
    q("UPDATE users SET tier=?, custom_limit=?, banned=? WHERE id=?",
      (tier, _custom(request.form.get("custom")), 1 if request.form.get("banned") else 0, uid), commit=True)
    flash(f"User {uid} updated", "ok")
    return redirect(request.referrer or "/users")


@app.route("/api", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        base = request.form.get("base", "").strip().rstrip("/")
        base = re.split(r"/api(/|$)", base)[0]  # user may paste the full endpoint
        if base and not base.startswith("http"):
            base = "https://" + base
        set_setting("api_base_url", base)
        tok = request.form.get("token", "").strip()
        if tok:
            set_setting("api_token", tok)
        set_setting("verify_ssl", "1" if request.form.get("verify") else "0")
        ok, info = test_api()
        flash(("✅ Connected successfully — " if ok else "❌ Connection failed — ") + info, "ok" if ok else "error")
        return redirect("/api")
    tok = get_setting("api_token")
    masked = (tok[:8] + "…" + tok[-4:]) if len(tok) > 14 else ("•••" if tok else "")
    body = """<div class="card"><h2>🔌 Reset API Connection</h2>
    <p>Status: {% if st=='connected' %}<b class="ok">🟢 Connected</b> <span class="pill">{{info}}</span>
    {% elif st=='failed' %}<b class="bad">🔴 Failed</b> <span class="pill">{{info}}</span>{% else %}<b class="warn">⚪ Not connected</b>{% endif %}</p>
    <form method="post"><input type="hidden" name="_c" value="{{csrf()}}">
    <div class="row"><div><label>API Base URL</label><input type="text" name="base" value="{{base}}" placeholder="https://140.245.202.76"></div>
    <div><label>API Token (leave blank to keep current: {{masked or 'none'}})</label><input type="password" name="token" placeholder="txr_..."></div></div>
    <label style="display:inline"><input type="checkbox" name="verify" {{'checked' if verify}}> Verify SSL certificate
    (leave OFF for a raw IP / self-signed https)</label><br><br>
    <button class="btn">Save &amp; Test Connection</button></form>
    <p style="color:var(--mu)">Reset endpoint used: <code>POST {{base or 'BASE'}}/api/seller/reset-hwid</code> with <code>{"license_key": "..."}</code></p></div>"""
    return page("API", "a", body, st=get_setting("api_status"), info=get_setting("api_info"),
                base=get_setting("api_base_url"), masked=masked, verify=get_setting("verify_ssl") == "1")


@app.route("/limits", methods=["GET", "POST"])
def limits():
    if request.method == "POST":
        for t in TIERS:
            try:
                set_setting("limit_" + t, int(request.form.get(t, "")))
            except ValueError:
                pass
        flash("Limits saved", "ok")
        return redirect("/limits")
    vals = {t: tier_limit(t) for t in TIERS}
    body = """<div class="card"><h2>⚙️ Default daily reset limits (rolling 24h)</h2>
    <form method="post"><input type="hidden" name="_c" value="{{csrf()}}"><div class="row">
    {% for t in tiers %}<div><label>{{t|capitalize}}</label><input type="number" name="{{t}}" value="{{vals[t]}}"></div>{% endfor %}</div>
    <p style="color:var(--mu)">Use <b>-1</b> for unlimited. Any user can also get a custom limit (e.g. 1000 / 2000) in the Users page.</p>
    <button class="btn">Save limits</button></form></div>"""
    return page("Limits", "l", body, tiers=TIERS, vals=vals)


@app.route("/logs")
def logs():
    rows = q("SELECT r.*, u.username FROM resets r LEFT JOIN users u ON u.id=r.user_id ORDER BY r.id DESC LIMIT 200")
    body = """<div class="card"><h2>Last 200 requests</h2><div class="scroll"><table>
    <tr><th>Time</th><th>User</th><th>Key</th><th>Status</th><th>Message</th></tr>
    {% for r in rows %}<tr><td>{{r.ts|ts}}</td><td>{{r.user_id}} {{('@'+r.username) if r.username}}</td><td><code>{{r.license_key}}</code></td>
    <td class="{{'ok' if r.status=='success' else 'bad'}}">{{r.status}}</td><td>{{r.message}}</td></tr>{% endfor %}</table></div></div>"""
    return page("Logs", "g", body, rows=rows)


# ------------------------------------------------------------------ background
def keepalive():
    url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("KEEPALIVE_URL")
    if not url:
        log.info("Keep-alive disabled (no RENDER_EXTERNAL_URL)")
        return
    log.info("Keep-alive pinging %s every 4 minutes", url)
    while True:
        time.sleep(240)
        try:
            requests.get(url.rstrip("/") + "/health", timeout=15)
        except Exception as e:
            log.warning("keep-alive failed: %s", e)


def run_bot():
    while True:
        try:
            bot.remove_webhook()
            log.info("Bot polling started")
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
        except Exception as e:
            log.error("polling crashed: %s", e)
            time.sleep(5)


init_db()

if __name__ == "__main__":
    if not BOT_TOKEN:
        log.error("BOT_TOKEN is missing!")
    else:
        threading.Thread(target=run_bot, daemon=True).start()
    threading.Thread(target=keepalive, daemon=True).start()
    if get_setting("api_base_url") and get_setting("api_token"):
        threading.Thread(target=test_api, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
