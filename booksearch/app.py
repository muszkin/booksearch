#!/usr/bin/env python3
"""
BookSearch — prosta wyszukiwarka ebookow
Szuka na Anna's Archive przez FlareSolverr, pobiera przez Stacks.
Parsuje HTML przez BeautifulSoup. Z systemem logowania + sesje.
"""
import os, re, json, secrets, hashlib, urllib.request, urllib.parse
from functools import wraps
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, redirect, make_response

from bs4 import BeautifulSoup

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://flaresolverr:8191/v1")
STACKS_URL = os.environ.get("STACKS_URL", "http://stacks:7788")
ANNAS_DOMAIN = os.environ.get("ANNAS_DOMAIN", "annas-archive.gl")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
KINDLE_SETTINGS_FILE = os.path.join(DATA_DIR, "kindle-settings.json")
NO_KINDLE_FILE = os.path.join(DATA_DIR, "no-kindle.txt")

# -- Auth helpers --------------------------------------------------------------

def _hash_pw(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return f"{salt}:{h}"

def _check_pw(password, stored):
    salt, _ = stored.split(":", 1)
    return _hash_pw(password, salt) == stored

def _load_users():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(USERS_FILE):
        return json.loads(open(USERS_FILE).read())
    default_user = os.environ.get("DEFAULT_USER", "admin")
    default_pass = os.environ.get("DEFAULT_PASS", "admin")
    users = {default_user: {"password": _hash_pw(default_pass), "created": datetime.utcnow().isoformat()}}
    open(USERS_FILE, "w").write(json.dumps(users, indent=2))
    return users

def _save_users(users):
    os.makedirs(DATA_DIR, exist_ok=True)
    open(USERS_FILE, "w").write(json.dumps(users, indent=2))

def _load_sessions():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(SESSIONS_FILE):
        return json.loads(open(SESSIONS_FILE).read())
    return {}

def _save_sessions(sessions):
    os.makedirs(DATA_DIR, exist_ok=True)
    open(SESSIONS_FILE, "w").write(json.dumps(sessions, indent=2))

def _get_current_user():
    token = request.cookies.get("session_token")
    if not token:
        return None
    sessions = _load_sessions()
    session = sessions.get(token)
    if not session:
        return None
    created = datetime.fromisoformat(session["created"])
    if (datetime.utcnow() - created).days > 30:
        del sessions[token]
        _save_sessions(sessions)
        return None
    return session["user"]

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = _get_current_user()
        if not user:
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

# -- Kindle settings helpers ---------------------------------------------------

def _load_kindle_settings():
    if os.path.exists(KINDLE_SETTINGS_FILE):
        return json.loads(open(KINDLE_SETTINGS_FILE).read())
    return {
        "kindle_email": "",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_email": "",
        "smtp_password": "",
        "enabled": False,
    }

def _save_kindle_settings(settings):
    os.makedirs(DATA_DIR, exist_ok=True)
    open(KINDLE_SETTINGS_FILE, "w").write(json.dumps(settings, indent=2))


# -- Search / Download --------------------------------------------------------

def flaresolverr_get(url, timeout=30):
    try:
        payload = json.dumps({"cmd": "request.get", "url": url, "maxTimeout": timeout * 1000}).encode()
        req = urllib.request.Request(FLARESOLVERR_URL, data=payload, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=timeout + 10)
        data = json.loads(resp.read())
        if data.get("status") == "ok":
            return data.get("solution", {}).get("response", "")
    except Exception as e:
        app.logger.error(f"FlareSolverr error: {e}")
    return ""

def search_annas(query, lang="", ext="epub"):
    params = {"q": query}
    if ext: params["ext"] = ext
    if lang: params["lang"] = lang
    url = f"https://{ANNAS_DOMAIN}/search?{urllib.parse.urlencode(params)}"
    app.logger.info(f"Searching: {url}")
    html = flaresolverr_get(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    results, seen = [], set()

    for link in soup.find_all("a", href=re.compile(r"/md5/[a-f0-9]{32}")):
        md5_m = re.search(r"/md5/([a-f0-9]{32})", link.get("href", ""))
        if not md5_m: continue
        md5 = md5_m.group(1)
        dc_divs = link.find_all(attrs={"data-content": True})
        if not dc_divs: continue
        if md5 in seen: continue
        seen.add(md5)

        title = dc_divs[0]["data-content"] if dc_divs else ""
        author = dc_divs[1]["data-content"] if len(dc_divs) > 1 else ""
        if not title or len(title) < 2: continue

        container = link.parent.parent if link.parent and link.parent.parent else (link.parent or link)
        text = container.get_text(separator=" ", strip=True)

        fmt_m = re.search(r"\b(EPUB|PDF|MOBI|AZW3|DJVU|FB2)\b", text)
        size_m = re.search(r"(\d+[\.,]\d+\s*MB|\d+\s*MB|\d+[\.,]\d+\s*KB|\d+\s*KB)", text, re.I)
        lang_m = re.search(r"\b(Polish|English|German|French|Russian|Spanish|Italian)\b", text)

        results.append({
            "md5": md5, "title": title.strip(), "author": author.strip(),
            "format": fmt_m.group(1).lower() if fmt_m else ext or "",
            "size": size_m.group(1) if size_m else "",
            "language": lang_m.group(1) if lang_m else "",
            "url": f"https://{ANNAS_DOMAIN}/md5/{md5}",
        })
        if len(results) >= 25: break
    return results

def download_via_stacks(md5):
    try:
        payload = json.dumps({"md5": md5}).encode()
        req = urllib.request.Request(f"{STACKS_URL}/api/queue/add", data=payload, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e), "success": False}


def _save_no_kindle(title):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(NO_KINDLE_FILE, "a", encoding="utf-8") as f:
        f.write(title.strip() + "\n")


# -- CSS (shared) --------------------------------------------------------------

SHARED_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0f0f0f; color: #e0e0e0; min-height: 100vh; }
.container { max-width: 800px; margin: 0 auto; padding: 20px; }
h1 { text-align: center; margin: 30px 0 20px; font-size: 28px; }
h1 span { font-size: 36px; }
input[type=text], input[type=password], input[type=email], input[type=number] {
    padding: 14px 18px; border-radius: 12px; border: 1px solid #333;
    background: #1a1a1a; color: #fff; font-size: 16px; outline: none; width: 100%; }
input:focus { border-color: #6c5ce7; }
.btn { padding: 14px 24px; border-radius: 12px; border: none;
    background: #6c5ce7; color: #fff; font-size: 16px; cursor: pointer;
    font-weight: 600; white-space: nowrap; width: 100%; }
.btn:hover { background: #5a4bd1; }
.btn:disabled { opacity: 0.5; cursor: wait; }
.btn-danger { background: #d63031; }
.btn-danger:hover { background: #c0392b; }
.btn-sm { padding: 8px 16px; font-size: 13px; width: auto; }
.card { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 12px;
    padding: 24px; margin-bottom: 16px; }
.form-group { margin-bottom: 16px; }
.form-group label { display: block; margin-bottom: 6px; color: #aaa; font-size: 14px; }
.error-msg { color: #d63031; font-size: 14px; margin-bottom: 12px; text-align: center; }
.success-msg { color: #00b894; font-size: 14px; margin-bottom: 12px; text-align: center; }
.topbar { display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 20px; padding: 0 0 10px 0; border-bottom: 1px solid #222; }
.topbar-user { color: #888; font-size: 13px; }
.topbar-links a { color: #6c5ce7; text-decoration: none; font-size: 13px; margin-left: 16px; }
.topbar-links a:hover { color: #a29bfe; }
.toggle { position: relative; display: inline-block; width: 50px; height: 26px; }
.toggle input { opacity: 0; width: 0; height: 0; }
.toggle-slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
    background: #333; border-radius: 26px; transition: 0.3s; }
.toggle-slider:before { content: ''; position: absolute; height: 20px; width: 20px;
    left: 3px; bottom: 3px; background: #fff; border-radius: 50%; transition: 0.3s; }
.toggle input:checked + .toggle-slider { background: #00b894; }
.toggle input:checked + .toggle-slider:before { transform: translateX(24px); }
"""

# -- Templates -----------------------------------------------------------------

LOGIN_TEMPLATE = """
<!DOCTYPE html><html lang="pl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BookSearch</title>
<style>""" + SHARED_CSS + """</style></head><body>
<div class="container" style="max-width:400px; margin-top: 80px;">
    <h1><span>📚</span> BookSearch</h1>
    <div class="card">
        {% if error %}<div class="error-msg">{{ error }}</div>{% endif %}
        <form method="POST" action="/login">
            <div class="form-group">
                <label>Uzytkownik</label>
                <input type="text" name="username" required autofocus>
            </div>
            <div class="form-group">
                <label>Haslo</label>
                <input type="password" name="password" required>
            </div>
            <button type="submit" class="btn">Zaloguj sie</button>
        </form>
    </div>
</div></body></html>
"""

SETTINGS_TEMPLATE = """
<!DOCTYPE html><html lang="pl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BookSearch — Ustawienia</title>
<style>""" + SHARED_CSS + """</style></head><body>
<div class="container" style="max-width:500px;">
    <div class="topbar">
        <div class="topbar-user">{{ user }}</div>
        <div class="topbar-links">
            <a href="/">Szukaj</a>
            <a href="/logout">Wyloguj</a>
        </div>
    </div>
    <h1>Ustawienia</h1>

    <div class="card">
        <h3 style="margin-bottom:16px; color:#fff;">Zmiana hasla</h3>
        {% if pw_error %}<div class="error-msg">{{ pw_error }}</div>{% endif %}
        {% if pw_success %}<div class="success-msg">{{ pw_success }}</div>{% endif %}
        <form method="POST" action="/settings">
            <input type="hidden" name="form_type" value="password">
            <div class="form-group">
                <label>Obecne haslo</label>
                <input type="password" name="current_password" required>
            </div>
            <div class="form-group">
                <label>Nowe haslo</label>
                <input type="password" name="new_password" required minlength="4">
            </div>
            <div class="form-group">
                <label>Powtorz nowe haslo</label>
                <input type="password" name="confirm_password" required minlength="4">
            </div>
            <button type="submit" class="btn">Zmien haslo</button>
        </form>
    </div>

    <div class="card">
        <h3 style="margin-bottom:16px; color:#fff;">Kindle — wysylanie ebookow</h3>
        {% if kindle_error %}<div class="error-msg">{{ kindle_error }}</div>{% endif %}
        {% if kindle_success %}<div class="success-msg">{{ kindle_success }}</div>{% endif %}
        <form method="POST" action="/settings">
            <input type="hidden" name="form_type" value="kindle">
            <div class="form-group" style="display:flex; align-items:center; gap:12px;">
                <label style="margin:0; flex:1;">Wlacz wysylanie na Kindle</label>
                <label class="toggle">
                    <input type="checkbox" name="kindle_enabled" value="1" {{ 'checked' if kindle.enabled }}>
                    <span class="toggle-slider"></span>
                </label>
            </div>
            <div class="form-group">
                <label>Adres Kindle (np. user@kindle.com)</label>
                <input type="email" name="kindle_email" value="{{ kindle.kindle_email }}" placeholder="user@kindle.com">
            </div>
            <div class="form-group">
                <label>SMTP Host</label>
                <input type="text" name="smtp_host" value="{{ kindle.smtp_host }}" placeholder="smtp.gmail.com">
            </div>
            <div class="form-group">
                <label>SMTP Port</label>
                <input type="number" name="smtp_port" value="{{ kindle.smtp_port }}" placeholder="587">
            </div>
            <div class="form-group">
                <label>SMTP Email (adres nadawcy)</label>
                <input type="email" name="smtp_email" value="{{ kindle.smtp_email }}" placeholder="sender@gmail.com">
            </div>
            <div class="form-group">
                <label>SMTP Haslo (App Password)</label>
                <input type="password" name="smtp_password" value="{{ kindle.smtp_password }}" placeholder="app-password">
            </div>
            <button type="submit" class="btn">Zapisz ustawienia Kindle</button>
        </form>
    </div>
</div></body></html>
"""

MAIN_TEMPLATE = """
<!DOCTYPE html><html lang="pl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BookSearch</title>
<style>""" + SHARED_CSS + """
.search-box { display: flex; gap: 10px; margin-bottom: 10px; }
.search-box input { flex: 1; }
.search-box button { width: auto; }
.filters { display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; }
.filters select { padding: 8px 12px; border-radius: 8px; border: 1px solid #333;
    background: #1a1a1a; color: #e0e0e0; font-size: 14px; }
.status { text-align: center; padding: 40px; color: #888; }
.spinner { display: inline-block; width: 24px; height: 24px;
    border: 3px solid #333; border-top-color: #6c5ce7;
    border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.results { display: flex; flex-direction: column; gap: 12px; padding-bottom: 80px; }
.result { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 12px;
    padding: 16px; transition: border-color 0.2s; display: flex; gap: 12px; align-items: flex-start; }
.result:hover { border-color: #6c5ce7; }
.result.selected { border-color: #6c5ce7; background: #1e1a2e; }
.result-checkbox { margin-top: 4px; width: 18px; height: 18px; accent-color: #6c5ce7; cursor: pointer; flex-shrink: 0; }
.result-body { flex: 1; min-width: 0; }
.result-title { font-size: 16px; font-weight: 600; margin-bottom: 4px; color: #fff; }
.result-author { font-size: 14px; color: #aaa; margin-bottom: 8px; }
.result-meta { font-size: 13px; color: #888; margin-bottom: 10px;
    display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 6px;
    font-size: 11px; font-weight: 600; text-transform: uppercase; }
.tag-epub { background: rgba(108,92,231,0.2); color: #a29bfe; }
.tag-pdf { background: rgba(214,48,49,0.2); color: #fab1a0; }
.tag-mobi { background: rgba(0,206,209,0.2); color: #81ecec; }
.tag-azw3 { background: rgba(0,206,209,0.2); color: #81ecec; }
.tag-lang { background: rgba(253,203,110,0.15); color: #fdcb6e; }
.result-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.btn-calibre { padding: 8px 16px; border-radius: 8px; border: none;
    background: #6c5ce7; color: #fff; font-size: 13px; cursor: pointer; font-weight: 600; }
.btn-calibre:hover { background: #5a4bd1; }
.btn-calibre:disabled { opacity: 0.5; cursor: wait; }
.btn-calibre.done { background: #636e72; }
.btn-kindle { padding: 8px 16px; border-radius: 8px; border: none;
    background: #00b894; color: #fff; font-size: 13px; cursor: pointer; font-weight: 600; }
.btn-kindle:hover { background: #00a381; }
.btn-kindle:disabled { opacity: 0.5; cursor: wait; }
.btn-kindle.done { background: #636e72; }
.btn-link { color: #888; font-size: 12px; text-decoration: none; }
.btn-link:hover { color: #aaa; }
.toast { position: fixed; bottom: 20px; right: 20px; padding: 12px 20px;
    border-radius: 10px; background: #00b894; color: #fff; font-weight: 600;
    font-size: 14px; display: none; z-index: 200; box-shadow: 0 4px 12px rgba(0,0,0,0.3); }
.toast.error { background: #d63031; }
.bulk-bar { position: fixed; bottom: 0; left: 0; right: 0; background: #1a1a1a;
    border-top: 1px solid #333; padding: 12px 20px; display: none; z-index: 150;
    justify-content: center; align-items: center; gap: 16px; box-shadow: 0 -4px 16px rgba(0,0,0,0.5); }
.bulk-bar.visible { display: flex; }
.bulk-count { color: #e0e0e0; font-size: 14px; font-weight: 600; }
.bulk-btn-calibre { padding: 10px 20px; border-radius: 8px; border: none;
    background: #6c5ce7; color: #fff; font-size: 14px; cursor: pointer; font-weight: 600; }
.bulk-btn-calibre:hover { background: #5a4bd1; }
.bulk-btn-calibre:disabled { opacity: 0.5; cursor: wait; }
.bulk-btn-kindle { padding: 10px 20px; border-radius: 8px; border: none;
    background: #00b894; color: #fff; font-size: 14px; cursor: pointer; font-weight: 600; }
.bulk-btn-kindle:hover { background: #00a381; }
.bulk-btn-kindle:disabled { opacity: 0.5; cursor: wait; }
.footer { text-align: center; padding: 30px; color: #555; font-size: 12px; }
</style></head><body>
<div class="container">
    <div class="topbar">
        <div class="topbar-user">{{ user }}</div>
        <div class="topbar-links">
            <a href="/settings">Ustawienia</a>
            <a href="/logout">Wyloguj</a>
        </div>
    </div>

    <h1><span>📚</span> BookSearch</h1>

    <div class="search-box">
        <input type="text" id="q" placeholder="Szukaj ksiazki..." autofocus
               onkeydown="if(event.key==='Enter')doSearch()">
        <button onclick="doSearch()" id="search-btn" class="btn">Szukaj</button>
    </div>

    <div class="filters">
        <select id="lang">
            <option value="">🌍 Wszystkie jezyki</option>
            <option value="pl" selected>🇵🇱 Polski</option>
            <option value="en">🇬🇧 English</option>
            <option value="de">🇩🇪 Deutsch</option>
            <option value="ru">🇷🇺 Русский</option>
        </select>
        <select id="ext">
            <option value="epub">EPUB</option>
            <option value="">Wszystkie formaty</option>
            <option value="pdf">PDF</option>
            <option value="mobi">MOBI</option>
        </select>
    </div>

    <div id="results">
        <div class="status">Wpisz tytul lub autora i kliknij Szukaj</div>
    </div>

    <div class="footer">
        BookSearch &rarr; Anna's Archive &rarr; Stacks &rarr; Calibre &rarr; Kindle<br>
        Szukanie trwa 10-20s (Cloudflare challenge)
    </div>
</div>

<div class="bulk-bar" id="bulk-bar">
    <span class="bulk-count" id="bulk-count">Zaznaczono: 0</span>
    <button class="bulk-btn-calibre" id="bulk-calibre" onclick="bulkDownload(false)">📚 Calibre All</button>
    <button class="bulk-btn-kindle" id="bulk-kindle" onclick="bulkDownload(true)">📱 Kindle All</button>
</div>

<div class="toast" id="toast"></div>

<script>
let searchResults = [];

async function doSearch() {
    const q = document.getElementById('q').value.trim();
    if (!q) return;
    const lang = document.getElementById('lang').value;
    const ext = document.getElementById('ext').value;
    const btn = document.getElementById('search-btn');
    const results = document.getElementById('results');
    searchResults = [];
    updateBulkBar();
    btn.disabled = true; btn.textContent = 'Szukam...';
    results.innerHTML = '<div class="status"><div class="spinner"></div><br><br>Szukam na Anna\\'s Archive...<br><small style="color:#666">Cloudflare challenge — 10-20 sekund</small></div>';
    try {
        const resp = await fetch('/api/search?' + new URLSearchParams({q, lang, ext}));
        if (resp.status === 401) { location.href = '/login'; return; }
        const data = await resp.json();
        if (data.error) {
            results.innerHTML = '<div class="status">' + esc(data.error) + '</div>';
        } else if (data.length === 0) {
            results.innerHTML = '<div class="status">Brak wynikow.<br><small>Sprobuj inna fraze, inny jezyk lub format.</small></div>';
        } else {
            searchResults = data;
            results.innerHTML = data.map((r, i) => `
                <div class="result" id="result-${i}">
                    <input type="checkbox" class="result-checkbox" data-idx="${i}" onchange="toggleSelect(${i}, this.checked)">
                    <div class="result-body">
                        <div class="result-title">${esc(r.title)}</div>
                        ${r.author ? '<div class="result-author">' + esc(r.author) + '</div>' : ''}
                        <div class="result-meta">
                            ${r.format ? '<span class="tag tag-' + r.format + '">' + r.format.toUpperCase() + '</span>' : ''}
                            ${r.language ? '<span class="tag tag-lang">' + esc(r.language) + '</span>' : ''}
                            ${r.size ? '<span>' + esc(r.size) + '</span>' : ''}
                        </div>
                        <div class="result-actions">
                            <button class="btn-calibre" id="cal-${i}" onclick="doDownload('${r.md5}', ${i}, false)">📚 Calibre</button>
                            <button class="btn-kindle" id="kin-${i}" onclick="doDownload('${r.md5}', ${i}, true)">📱 Kindle</button>
                            <a href="${esc(r.url)}" target="_blank" class="btn-link">Anna's Archive</a>
                        </div>
                    </div>
                </div>`).join('');
        }
    } catch (e) { results.innerHTML = '<div class="status">' + esc(e.message) + '</div>'; }
    btn.disabled = false; btn.textContent = 'Szukaj';
}

async function doDownload(md5, idx, sendToKindle) {
    const btnId = sendToKindle ? 'kin-' + idx : 'cal-' + idx;
    const btn = document.getElementById(btnId);
    const title = searchResults[idx] ? searchResults[idx].title : '';
    btn.disabled = true; btn.textContent = sendToKindle ? 'Wysylam...' : 'Pobieram...';
    try {
        const resp = await fetch('/api/download', {method:'POST', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({md5, send_to_kindle: sendToKindle, title})});
        if (resp.status === 401) { location.href = '/login'; return; }
        const data = await resp.json();
        if (data.success) {
            btn.textContent = 'W kolejce!';
            btn.className = (sendToKindle ? 'btn-kindle' : 'btn-calibre') + ' done';
            showToast(sendToKindle ? 'Pobrano + Kindle' : 'Pobrano do Calibre');
        } else {
            btn.textContent = 'Blad';
            showToast(data.error||'Nie udalo sie', true);
            setTimeout(()=>{ btn.disabled=false; btn.textContent = sendToKindle ? '📱 Kindle' : '📚 Calibre'; }, 3000);
        }
    } catch(e) {
        btn.textContent='Blad'; showToast(e.message,true);
        setTimeout(()=>{ btn.disabled=false; btn.textContent = sendToKindle ? '📱 Kindle' : '📚 Calibre'; }, 3000);
    }
}

function toggleSelect(idx, checked) {
    const el = document.getElementById('result-' + idx);
    if (checked) { el.classList.add('selected'); } else { el.classList.remove('selected'); }
    updateBulkBar();
}

function getSelectedItems() {
    const checkboxes = document.querySelectorAll('.result-checkbox:checked');
    return Array.from(checkboxes).map(cb => {
        const idx = parseInt(cb.dataset.idx);
        return { md5: searchResults[idx].md5, title: searchResults[idx].title, idx };
    });
}

function updateBulkBar() {
    const selected = document.querySelectorAll('.result-checkbox:checked').length;
    const bar = document.getElementById('bulk-bar');
    const count = document.getElementById('bulk-count');
    if (selected > 0) {
        bar.classList.add('visible');
        count.textContent = 'Zaznaczono: ' + selected;
    } else {
        bar.classList.remove('visible');
    }
}

async function bulkDownload(sendToKindle) {
    const items = getSelectedItems();
    if (items.length === 0) return;
    const btnId = sendToKindle ? 'bulk-kindle' : 'bulk-calibre';
    const btn = document.getElementById(btnId);
    btn.disabled = true;
    const label = sendToKindle ? '📱 Kindle All' : '📚 Calibre All';
    btn.textContent = '0/' + items.length + '...';
    try {
        const payload = items.map(it => ({ md5: it.md5, send_to_kindle: sendToKindle, title: it.title }));
        const resp = await fetch('/api/download/bulk', {method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({items: payload})});
        if (resp.status === 401) { location.href = '/login'; return; }
        const data = await resp.json();
        const ok = data.results.filter(r => r.success).length;
        const fail = data.results.length - ok;
        showToast(ok + ' pobrano' + (fail ? ', ' + fail + ' bledow' : ''));
        data.results.forEach((r, i) => {
            const idx = items[i].idx;
            const calBtn = document.getElementById('cal-' + idx);
            const kinBtn = document.getElementById('kin-' + idx);
            if (r.success) {
                if (sendToKindle && kinBtn) { kinBtn.textContent = 'W kolejce!'; kinBtn.className = 'btn-kindle done'; kinBtn.disabled = true; }
                if (!sendToKindle && calBtn) { calBtn.textContent = 'W kolejce!'; calBtn.className = 'btn-calibre done'; calBtn.disabled = true; }
            }
        });
    } catch(e) { showToast(e.message, true); }
    btn.disabled = false; btn.textContent = label;
}

function showToast(msg,isError) { const t=document.getElementById('toast'); t.textContent=msg; t.className='toast'+(isError?' error':''); t.style.display='block'; setTimeout(()=>t.style.display='none',4000); }
function esc(s) { return s ? String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') : ''; }
</script></body></html>
"""


# -- Routes --------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        users = _load_users()
        if username in users and _check_pw(password, users[username]["password"]):
            token = secrets.token_hex(32)
            sessions = _load_sessions()
            sessions[token] = {"user": username, "created": datetime.utcnow().isoformat()}
            _save_sessions(sessions)
            resp = make_response(redirect("/"))
            resp.set_cookie("session_token", token, max_age=30*24*3600, httponly=True, samesite="Lax")
            return resp
        error = "Nieprawidlowy login lub haslo"
    return render_template_string(LOGIN_TEMPLATE, error=error)

@app.route("/logout")
def logout():
    token = request.cookies.get("session_token")
    if token:
        sessions = _load_sessions()
        sessions.pop(token, None)
        _save_sessions(sessions)
    resp = make_response(redirect("/login"))
    resp.delete_cookie("session_token")
    return resp

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user = _get_current_user()
    pw_error, pw_success = "", ""
    kindle_error, kindle_success = "", ""
    kindle = _load_kindle_settings()

    if request.method == "POST":
        form_type = request.form.get("form_type", "")

        if form_type == "password":
            current = request.form.get("current_password", "")
            new_pw = request.form.get("new_password", "")
            confirm = request.form.get("confirm_password", "")
            users = _load_users()
            if user not in users or not _check_pw(current, users[user]["password"]):
                pw_error = "Obecne haslo jest nieprawidlowe"
            elif len(new_pw) < 4:
                pw_error = "Nowe haslo musi miec min. 4 znaki"
            elif new_pw != confirm:
                pw_error = "Hasla nie sa takie same"
            else:
                users[user]["password"] = _hash_pw(new_pw)
                _save_users(users)
                pw_success = "Haslo zmienione!"

        elif form_type == "kindle":
            kindle = {
                "kindle_email": request.form.get("kindle_email", "").strip(),
                "smtp_host": request.form.get("smtp_host", "smtp.gmail.com").strip(),
                "smtp_port": int(request.form.get("smtp_port", 587)),
                "smtp_email": request.form.get("smtp_email", "").strip(),
                "smtp_password": request.form.get("smtp_password", ""),
                "enabled": request.form.get("kindle_enabled") == "1",
            }
            _save_kindle_settings(kindle)
            kindle_success = "Ustawienia Kindle zapisane!"

    return render_template_string(
        SETTINGS_TEMPLATE, user=user,
        pw_error=pw_error, pw_success=pw_success,
        kindle_error=kindle_error, kindle_success=kindle_success,
        kindle=type("K", (), kindle)(),
    )

@app.route("/")
@login_required
def index():
    user = _get_current_user()
    return render_template_string(MAIN_TEMPLATE, user=user)

@app.route("/api/search")
@login_required
def api_search():
    q = request.args.get("q", "").strip()
    lang = request.args.get("lang", "")
    ext = request.args.get("ext", "epub")
    if not q: return jsonify([])
    try:
        return jsonify(search_annas(q, lang=lang, ext=ext))
    except Exception as e:
        app.logger.error(f"Search error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/download", methods=["POST"])
@login_required
def api_download():
    data = request.get_json() or {}
    md5 = data.get("md5", "")
    if not md5:
        return jsonify({"error": "No MD5", "success": False})
    send_to_kindle = data.get("send_to_kindle", True)
    title = data.get("title", "")
    result = download_via_stacks(md5)
    if not send_to_kindle and title:
        _save_no_kindle(title)
    return jsonify(result)


@app.route("/api/download/bulk", methods=["POST"])
@login_required
def api_download_bulk():
    data = request.get_json() or {}
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "No items", "results": []})
    results = []
    for item in items:
        md5 = item.get("md5", "")
        if not md5:
            results.append({"error": "No MD5", "success": False})
            continue
        send_to_kindle = item.get("send_to_kindle", True)
        title = item.get("title", "")
        result = download_via_stacks(md5)
        if not send_to_kindle and title:
            _save_no_kindle(title)
        results.append(result)
    return jsonify({"results": results})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
