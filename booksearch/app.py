#!/usr/bin/env python3
"""
BookSearch — prosta wyszukiwarka ebookow
Szuka na Anna's Archive przez FlareSolverr, pobiera przez Stacks.
Parsuje HTML przez BeautifulSoup. Z systemem logowania + sesje.
v0.4: Kindle sending wbudowany w aplikacje.
v0.5: Calibre Library Browser + ZIP export.
v0.6: Format conversion, Activity Logs, Stacks queue integration.
"""
import os, re, json, secrets, hashlib, sqlite3, time, unicodedata, urllib.request, urllib.parse
import smtplib, threading, zipfile, io, subprocess, tempfile
from functools import wraps
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from flask import Flask, request, jsonify, render_template_string, redirect, make_response, send_file

from bs4 import BeautifulSoup

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://flaresolverr:8191/v1")
STACKS_URL = os.environ.get("STACKS_URL", "http://stacks:7788")
STACKS_USER = os.environ.get("STACKS_USER", "admin")
STACKS_PASS = os.environ.get("STACKS_PASS", "mucha2024")
ANNAS_DOMAIN = os.environ.get("ANNAS_DOMAIN", "annas-archive.gl")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
KINDLE_SETTINGS_FILE = os.path.join(DATA_DIR, "kindle-settings.json")  # legacy, for migration
KINDLE_QUEUE_JSON = os.path.join(DATA_DIR, "kindle-queue.json")
CALIBRE_SETTINGS_FILE_PATH = os.path.join(DATA_DIR, "calibre-settings.json")
CALIBRE_LIBRARY_PATH = os.environ.get("CALIBRE_LIBRARY_PATH", "/library")
ACTIVITY_LOG_FILE = os.path.join(DATA_DIR, "activity-log.json")
STACKS_SEEN_FILE = os.path.join(DATA_DIR, "stacks-seen.json")

MAX_LOG_ENTRIES = 500

# Thread lock for queue and log operations (shared)
_queue_lock = threading.Lock()

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
                _log_activity(
                    "auth_error", "", "",
                    f"Unauthorized API access: {request.method} {request.path} from {request.remote_addr}"
                )
                return jsonify({"error": "Unauthorized"}), 401
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

# -- Activity Log helpers ------------------------------------------------------

def _load_activity_log():
    """Load activity log from JSON file. Returns list."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(ACTIVITY_LOG_FILE):
        try:
            return json.loads(open(ACTIVITY_LOG_FILE).read())
        except Exception:
            return []
    return []

def _save_activity_log(log):
    """Save activity log to JSON file."""
    os.makedirs(DATA_DIR, exist_ok=True)
    open(ACTIVITY_LOG_FILE, "w").write(json.dumps(log, indent=2))

def _log_activity(type_, title, author, details, user=None, md5=None):
    """Add a log entry. Thread-safe. Trims to MAX_LOG_ENTRIES."""
    # Truncate very long details to prevent log bloat
    details = details or ""
    if len(details) > 500:
        details = details[:200] + " ... " + details[-200:]
    with _queue_lock:
        log = _load_activity_log()
        entry = {
            "timestamp": datetime.utcnow().isoformat(timespec="seconds"),
            "type": type_,
            "title": title or "",
            "author": author or "",
            "details": details,
            "user": user or "",
            "md5": md5 or "",
        }
        log.append(entry)
        # Trim oldest entries if over limit
        if len(log) > MAX_LOG_ENTRIES:
            log = log[-MAX_LOG_ENTRIES:]
        _save_activity_log(log)

# -- Stacks seen helpers -------------------------------------------------------

def _load_stacks_seen():
    """Load set of seen Stacks completion IDs."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(STACKS_SEEN_FILE):
        try:
            return set(json.loads(open(STACKS_SEEN_FILE).read()))
        except Exception:
            return set()
    return set()

def _save_stacks_seen(seen_set):
    """Save set of seen Stacks completion IDs."""
    os.makedirs(DATA_DIR, exist_ok=True)
    open(STACKS_SEEN_FILE, "w").write(json.dumps(list(seen_set)))

# -- Per-user Kindle settings helpers ------------------------------------------

def _get_user_kindle_settings(username):
    """Get Kindle settings for a specific user. Returns defaults if not configured."""
    users = _load_users()
    user_data = users.get(username, {})
    return user_data.get("kindle_settings", {
        "kindle_email": "",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_email": "",
        "smtp_password": "",
        "enabled": False,
    })

def _save_user_kindle_settings(username, settings):
    """Save Kindle settings for a specific user."""
    users = _load_users()
    if username not in users:
        return
    users[username]["kindle_settings"] = settings
    _save_users(users)

def _migrate_global_kindle_settings():
    """One-time migration: if old kindle-settings.json exists, copy to first user."""
    if not os.path.exists(KINDLE_SETTINGS_FILE):
        return
    try:
        old_settings = json.loads(open(KINDLE_SETTINGS_FILE).read())
        users = _load_users()
        if not users:
            return
        first_user = next(iter(users))
        if "kindle_settings" not in users[first_user]:
            app.logger.info(f"Migrating global Kindle settings to user '{first_user}'")
            users[first_user]["kindle_settings"] = old_settings
            _save_users(users)
            os.rename(KINDLE_SETTINGS_FILE, KINDLE_SETTINGS_FILE + ".migrated")
    except Exception as e:
        app.logger.error(f"Kindle settings migration error: {e}")

# -- Kindle send queue helpers -------------------------------------------------

def _load_kindle_queue():
    """Load the Kindle send queue from JSON file."""
    with _queue_lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(KINDLE_QUEUE_JSON):
            try:
                return json.loads(open(KINDLE_QUEUE_JSON).read())
            except Exception:
                return []
        return []

def _save_kindle_queue(queue):
    """Save the Kindle send queue to JSON file. Caller must hold _queue_lock."""
    os.makedirs(DATA_DIR, exist_ok=True)
    open(KINDLE_QUEUE_JSON, "w").write(json.dumps(queue, indent=2))

def _add_to_kindle_queue(md5, title, author, fmt, user, target_format="epub"):
    """Add a book to the Kindle send queue."""
    queue = _load_kindle_queue()
    # Check if already in queue
    for item in queue:
        if item["md5"] == md5:
            # Reset if failed
            if item["status"] in ("failed",):
                item["status"] = "pending"
                item["error"] = None
                item["attempts"] = 0
                with _queue_lock:
                    _save_kindle_queue(queue)
            return
    queue.append({
        "md5": md5,
        "title": title,
        "author": author,
        "format": fmt,
        "target_format": target_format,
        "user": user,
        "status": "pending",
        "added_at": datetime.utcnow().isoformat(),
        "sent_at": None,
        "error": None,
        "attempts": 0,
    })
    with _queue_lock:
        _save_kindle_queue(queue)

def _update_queue_item(queue, md5, **kwargs):
    """Update a queue item in place."""
    for item in queue:
        if item["md5"] == md5:
            item.update(kwargs)
            return

# -- Text normalization --------------------------------------------------------

def normalize_text(s):
    """Normalize text: strip diacritics, punctuation, collapse whitespace."""
    nfkd = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", stripped)
    return re.sub(r"\s+", " ", cleaned).lower().strip()


# -- Calibre settings helpers --------------------------------------------------

def _load_calibre_settings():
    if os.path.exists(CALIBRE_SETTINGS_FILE_PATH):
        return json.loads(open(CALIBRE_SETTINGS_FILE_PATH).read())
    return {"library_path": CALIBRE_LIBRARY_PATH}

def _save_calibre_settings(settings):
    os.makedirs(DATA_DIR, exist_ok=True)
    open(CALIBRE_SETTINGS_FILE_PATH, "w").write(json.dumps(settings, indent=2))

def _get_calibre_db_path():
    settings = _load_calibre_settings()
    return os.path.join(settings.get("library_path", CALIBRE_LIBRARY_PATH), "metadata.db")


def _load_calibre_books():
    db_path = _get_calibre_db_path()
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.execute(
            "SELECT b.title, a.name FROM books b "
            "JOIN books_authors_link bal ON b.id = bal.book "
            "JOIN authors a ON bal.author = a.id"
        )
        rows = cursor.fetchall()
        conn.close()
        return rows
    except Exception as e:
        app.logger.error(f"Calibre DB error: {e}")
        return []


def check_calibre_status(title, author):
    books = _load_calibre_books()
    if not books:
        return None
    norm_title = normalize_text(title)
    norm_author = normalize_text(author) if author else ""
    title_match = False
    author_match = False
    for book_title, book_author in books:
        bt = normalize_text(book_title)
        ba = normalize_text(book_author)
        if bt == norm_title and ba == norm_author:
            return "exact"
        if bt == norm_title:
            title_match = True
        if norm_author and ba == norm_author:
            author_match = True
    if title_match:
        return "title"
    if author_match:
        return "author"
    return None


def find_book_in_calibre(title, author, fmt="epub"):
    """Search Calibre library for the book by title and return filepath or None."""
    db_path = _get_calibre_db_path()
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.execute(
            "SELECT b.id, b.title, b.path, d.format, d.name "
            "FROM books b "
            "JOIN data d ON b.id = d.book "
            "WHERE d.format = ? COLLATE NOCASE",
            (fmt.upper(),)
        )
        rows = cursor.fetchall()
        conn.close()

        norm_title = normalize_text(title)
        library_path = _load_calibre_settings().get("library_path", CALIBRE_LIBRARY_PATH)
        for book_id, book_title, book_path, book_fmt, book_name in rows:
            if normalize_text(book_title) == norm_title:
                filepath = os.path.join(
                    library_path,
                    book_path,
                    f"{book_name}.{book_fmt.lower()}"
                )
                if os.path.exists(filepath):
                    return filepath
        return None
    except Exception as e:
        app.logger.error(f"Calibre search error: {e}")
        return None


def find_book_in_calibre_any_format(title, author):
    """Find a book in Calibre in any available format. Returns (filepath, format) or (None, None)."""
    db_path = _get_calibre_db_path()
    if not os.path.exists(db_path):
        return None, None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.execute(
            "SELECT b.id, b.title, b.path, d.format, d.name "
            "FROM books b "
            "JOIN data d ON b.id = d.book",
        )
        rows = cursor.fetchall()
        conn.close()

        norm_title = normalize_text(title)
        library_path = _load_calibre_settings().get("library_path", CALIBRE_LIBRARY_PATH)
        for book_id, book_title, book_path, book_fmt, book_name in rows:
            if normalize_text(book_title) == norm_title:
                filepath = os.path.join(
                    library_path,
                    book_path,
                    f"{book_name}.{book_fmt.lower()}"
                )
                if os.path.exists(filepath):
                    return filepath, book_fmt.lower()
        return None, None
    except Exception as e:
        app.logger.error(f"Calibre search (any format) error: {e}")
        return None, None


def convert_book_format(src_path, target_fmt, title="", author=""):
    """
    Convert a book to target format using ebook-convert (Calibre CLI).
    Returns path to temp converted file, or None on failure.
    Caller is responsible for deleting the temp file.
    """
    try:
        suffix = f".{target_fmt.lower()}"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.close()
        cmd = ["ebook-convert", src_path, tmp.name]
        app.logger.info(f"Converting: {src_path} -> {tmp.name} ({target_fmt})")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0 and os.path.exists(tmp.name) and os.path.getsize(tmp.name) > 0:
            app.logger.info(f"Conversion successful: {tmp.name}")
            return tmp.name
        else:
            app.logger.error(f"ebook-convert failed (rc={result.returncode}): {result.stderr[:500]}")
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)
            return None
    except FileNotFoundError:
        app.logger.error("ebook-convert not found — calibre not installed in this environment")
        return None
    except Exception as e:
        app.logger.error(f"Conversion error: {e}")
        return None


# -- SMTP / Kindle send logic --------------------------------------------------

MIN_FILE_SIZE = 5 * 1024  # 5 KB minimum

def send_book_to_kindle(filepath, kindle_settings):
    """Send an ebook file to Kindle via SMTP email. Returns (True, None) on success, (False, error_str) on failure."""
    if not os.path.exists(filepath):
        err = f"File not found: {filepath}"
        app.logger.error(err)
        return False, err
    if os.path.getsize(filepath) < MIN_FILE_SIZE:
        err = f"File too small ({os.path.getsize(filepath)} bytes), skipping: {filepath}"
        app.logger.warning(err)
        return False, err

    filename = os.path.basename(filepath)
    app.logger.info(f"Sending to Kindle: {filename} ({os.path.getsize(filepath) // 1024} KB)")

    try:
        msg = MIMEMultipart()
        msg["From"] = kindle_settings["smtp_email"]
        msg["To"] = kindle_settings["kindle_email"]
        msg["Subject"] = ""

        part = MIMEBase("application", "epub+zip")
        with open(filepath, "rb") as f:
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
        msg.attach(part)

        with smtplib.SMTP(kindle_settings["smtp_host"], int(kindle_settings["smtp_port"])) as server:
            server.starttls()
            server.login(kindle_settings["smtp_email"], kindle_settings["smtp_password"])
            server.sendmail(kindle_settings["smtp_email"], kindle_settings["kindle_email"], msg.as_string())

        app.logger.info(f"Sent to Kindle: {filename}")
        return True, None

    except smtplib.SMTPAuthenticationError as e:
        err = f"SMTP authentication failed: {e}"
        app.logger.error(f"Kindle send error for {filename}: {err}")
        return False, err
    except smtplib.SMTPException as e:
        err = f"SMTP error: {type(e).__name__}: {e}"
        app.logger.error(f"Kindle send error for {filename}: {err}")
        return False, err
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        app.logger.error(f"Kindle send error for {filename}: {err}")
        return False, err


# -- Background polling thread -------------------------------------------------

def _poll_stacks_status():
    """Poll Stacks /api/status and log new completions."""
    try:
        req = urllib.request.Request(
            f"{STACKS_URL}/api/status",
            headers={"Accept": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
    except Exception as e:
        app.logger.debug(f"Stacks status poll failed (ok if Stacks unreachable): {e}")
        return

    recent_history = data.get("recent_history", [])
    if not recent_history:
        return

    seen = _load_stacks_seen()
    new_seen = set(seen)
    changed = False

    for item in recent_history:
        item_id = str(item.get("id", ""))
        if not item_id or item_id in seen:
            continue
        # New completion detected
        new_seen.add(item_id)
        changed = True
        title = item.get("title", item.get("filename", "?"))
        md5 = item.get("md5", "")
        success = item.get("success", item.get("status") == "completed")
        if success:
            _log_activity(
                "stacks_download",
                title, "",
                f"Stacks pobrał: {item.get('filename', title)}",
                md5=md5
            )
        else:
            error = item.get("error", "nieznany błąd")
            _log_activity(
                "stacks_fail",
                title, "",
                f"Stacks błąd pobierania: {error}",
                md5=md5
            )

    if changed:
        with _queue_lock:
            _save_stacks_seen(new_seen)


def kindle_poll_worker():
    """Check every 30s if queued books are in Calibre library, then send them. Also polls Stacks."""
    app.logger.info("Kindle poll worker started")
    stacks_counter = 0
    while True:
        try:
            time.sleep(30)
            stacks_counter += 1

            # Poll Stacks status every cycle (every 30s)
            _poll_stacks_status()

            queue = _load_kindle_queue()
            changed = False

            for item in queue:
                if item["status"] not in ("pending", "found"):
                    continue

                try:
                    target_fmt = item.get("target_format", item.get("format", "epub")).lower()
                    title = item["title"]
                    author = item.get("author", "")
                    user = item.get("user", "")

                    # First try to find in target format
                    filepath = find_book_in_calibre(title, author, target_fmt)
                    converted_path = None
                    actual_fmt = target_fmt

                    if not filepath:
                        # Try any format and convert
                        src_path, src_fmt = find_book_in_calibre_any_format(title, author)
                        if not src_path:
                            continue
                        if src_fmt.lower() == target_fmt.lower():
                            filepath = src_path
                        else:
                            # Need conversion
                            app.logger.info(f"Converting {title}: {src_fmt} -> {target_fmt}")
                            _log_activity(
                                "conversion",
                                title, author,
                                f"Konwersja formatu: {src_fmt.upper()} -> {target_fmt.upper()}",
                                user=user,
                                md5=item.get("md5", "")
                            )
                            converted_path = convert_book_format(src_path, target_fmt, title, author)
                            if converted_path:
                                filepath = converted_path
                                actual_fmt = target_fmt
                            else:
                                _log_activity(
                                    "conversion_fail",
                                    title, author,
                                    f"Błąd konwersji: {src_fmt.upper()} -> {target_fmt.upper()}",
                                    user=user,
                                    md5=item.get("md5", "")
                                )
                                # Fall back to original format
                                filepath = src_path
                                actual_fmt = src_fmt

                    if not filepath:
                        continue

                    app.logger.info(f"Found in Calibre: {title} -> {filepath}")
                    item["status"] = "sending"
                    changed = True
                    with _queue_lock:
                        _save_kindle_queue(queue)

                    users = _load_users()
                    user_data = users.get(user, {})
                    kindle_cfg = user_data.get("kindle_settings", {})

                    if not kindle_cfg.get("enabled"):
                        item["status"] = "failed"
                        item["error"] = "Kindle nie skonfigurowany dla tego uzytkownika"
                        _log_activity(
                            "kindle_fail",
                            title, author,
                            f"Kindle nie skonfigurowany dla użytkownika '{user}' — włącz Kindle w Ustawieniach",
                            user=user,
                            md5=item.get("md5", "")
                        )
                        changed = True
                        if converted_path and os.path.exists(converted_path):
                            os.unlink(converted_path)
                        with _queue_lock:
                            _save_kindle_queue(queue)
                        continue

                    success, send_error = send_book_to_kindle(filepath, kindle_cfg)
                    if success:
                        item["status"] = "sent"
                        item["sent_at"] = datetime.utcnow().isoformat()
                        item["error"] = None
                        _log_activity(
                            "kindle_send",
                            title, author,
                            f"Wysłano na Kindle ({actual_fmt.upper()})",
                            user=user,
                            md5=item.get("md5", "")
                        )
                    else:
                        item["attempts"] = item.get("attempts", 0) + 1
                        if item["attempts"] < 3:
                            item["status"] = "pending"
                            item["error"] = f"Blad wysylania (proba {item['attempts']}/3): {send_error}"
                            _log_activity(
                                "kindle_fail",
                                title, author,
                                f"Błąd wysyłania na Kindle (próba {item['attempts']}/3, {actual_fmt.upper()}): {send_error}",
                                user=user,
                                md5=item.get("md5", "")
                            )
                        else:
                            item["status"] = "failed"
                            item["error"] = f"Nie udalo sie wyslac po 3 probach: {send_error}"
                            _log_activity(
                                "kindle_fail",
                                title, author,
                                f"Błąd wysyłania po 3 próbach ({actual_fmt.upper()}): {send_error}",
                                user=user,
                                md5=item.get("md5", "")
                            )
                    changed = True

                    # Clean up temp converted file
                    if converted_path and os.path.exists(converted_path):
                        os.unlink(converted_path)

                    with _queue_lock:
                        _save_kindle_queue(queue)

                except Exception as e:
                    app.logger.error(f"Error processing queue item {item.get('md5', '?')}: {e}")
                    _log_activity(
                        "kindle_fail",
                        item.get("title", ""),
                        item.get("author", ""),
                        f"Wyjątek przy przetwarzaniu kolejki Kindle: {type(e).__name__}: {e}",
                        user=item.get("user", ""),
                        md5=item.get("md5", "")
                    )
                    item["status"] = "failed"
                    item["error"] = str(e)
                    changed = True
                    with _queue_lock:
                        _save_kindle_queue(queue)

        except Exception as e:
            app.logger.error(f"Kindle poll worker error: {e}")


# -- Search / Download --------------------------------------------------------

def flaresolverr_get(url, timeout=60, retries=2):
    """Fetch URL via FlareSolverr with retry logic and increased timeout."""
    last_error = "empty response"
    for attempt in range(1, retries + 1):
        try:
            payload = json.dumps({"cmd": "request.get", "url": url, "maxTimeout": timeout * 1000}).encode()
            req = urllib.request.Request(FLARESOLVERR_URL, data=payload, headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=timeout + 15)
            data = json.loads(resp.read())
            status = data.get("status")
            if status == "ok":
                html = data.get("solution", {}).get("response", "")
                if html:
                    return html
                last_error = "empty response from FlareSolverr"
                app.logger.warning(f"FlareSolverr returned empty response for {url} (attempt {attempt}/{retries})")
            else:
                msg = data.get("message", "unknown")
                last_error = f"status={status} message={msg}"
                app.logger.warning(f"FlareSolverr status={status} message={msg} for {url} (attempt {attempt}/{retries})")
        except urllib.error.URLError as e:
            last_error = str(e)
            app.logger.error(f"FlareSolverr URL error (attempt {attempt}/{retries}): {e}")
        except Exception as e:
            last_error = str(e)
            app.logger.error(f"FlareSolverr error (attempt {attempt}/{retries}): {e}")
        if attempt < retries:
            time.sleep(2)
    _log_activity(
        "search_error", "", "",
        f"FlareSolverr failed after {retries} retries for URL: {url} — {last_error}"
    )
    return ""


def _parse_results_from_html(html, ext=""):
    """Parse search results from Anna's Archive HTML page."""
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
    return results, seen


def search_annas(query, lang="", ext="epub", max_pages=3):
    """Search Anna's Archive with pagination support and increased timeout."""
    params = {"q": query}
    if ext: params["ext"] = ext
    if lang: params["lang"] = lang

    all_results = []
    seen = set()

    for page in range(1, max_pages + 1):
        page_params = dict(params)
        if page > 1:
            page_params["page"] = str(page)
        url = f"https://{ANNAS_DOMAIN}/search?{urllib.parse.urlencode(page_params)}"
        app.logger.info(f"Searching page {page}: {url}")

        html = flaresolverr_get(url, timeout=60)
        if not html:
            app.logger.warning(f"Empty response for page {page}, stopping pagination")
            break

        page_results, page_seen = _parse_results_from_html(html, ext)

        new_count = 0
        for r in page_results:
            if r["md5"] not in seen:
                seen.add(r["md5"])
                all_results.append(r)
                new_count += 1

        app.logger.info(f"Page {page}: {len(page_results)} parsed, {new_count} new (total: {len(all_results)})")

        if len(all_results) >= 50:
            all_results = all_results[:50]
            break

        if new_count == 0:
            app.logger.info(f"No new results on page {page}, stopping pagination")
            break

    return all_results

_stacks_cookie_jar = urllib.request.HTTPCookieProcessor()
_stacks_opener = urllib.request.build_opener(_stacks_cookie_jar)
_stacks_authenticated = False

def _stacks_login():
    """Login to Stacks and store session cookie."""
    global _stacks_authenticated
    try:
        payload = json.dumps({"username": STACKS_USER, "password": STACKS_PASS}).encode()
        req = urllib.request.Request(
            f"{STACKS_URL}/login", data=payload,
            headers={"Content-Type": "application/json"}
        )
        resp = _stacks_opener.open(req, timeout=10)
        result = json.loads(resp.read())
        if result.get("success"):
            _stacks_authenticated = True
            app.logger.info("Stacks login successful")
            return True
        app.logger.error(f"Stacks login failed: {result}")
        return False
    except Exception as e:
        app.logger.error(f"Stacks login error: {e}")
        return False

def download_via_stacks(md5):
    global _stacks_authenticated
    try:
        payload = json.dumps({"md5": md5}).encode()

        # Try with current session
        if not _stacks_authenticated:
            _stacks_login()

        req = urllib.request.Request(
            f"{STACKS_URL}/api/queue/add", data=payload,
            headers={"Content-Type": "application/json"}
        )
        try:
            resp = _stacks_opener.open(req, timeout=10)
            return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # Session expired — re-login and retry once
                _stacks_authenticated = False
                if _stacks_login():
                    resp = _stacks_opener.open(req, timeout=10)
                    return json.loads(resp.read())
            raise

    except Exception as e:
        _log_activity(
            "download_error", "", "",
            f"Stacks queue error for md5={md5}: {type(e).__name__}: {e}"
        )
        return {"error": str(e), "success": False}


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
select { padding: 8px 12px; border-radius: 8px; border: 1px solid #333;
    background: #1a1a1a; color: #e0e0e0; font-size: 14px; outline: none; }
select:focus { border-color: #6c5ce7; }
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

# -- TOPBAR snippet (shared) --------------------------------------------------
# Used in all templates; order: Szukaj | Biblioteka | Kolejka Kindle | Logi | Ustawienia | Wyloguj
TOPBAR_LINKS = """
            <a href="/">Szukaj</a>
            <a href="/library">Biblioteka</a>
            <a href="/kindle-queue">📱 Kolejka Kindle</a>
            <a href="/logs">📋 Logi</a>
            <a href="/settings">Ustawienia</a>
            <a href="/logout">Wyloguj</a>
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
        <div class="topbar-links">""" + TOPBAR_LINKS + """</div>
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
        <p style="color:#888; font-size:13px; margin-bottom:16px;">Ustawienia per uzytkownik — kazdy konfiguruje swoj wlasny Kindle.</p>
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

    <div class="card">
        <h3 style="margin-bottom:16px; color:#fff;">Calibre — integracja z biblioteka</h3>
        {% if calibre_error %}<div class="error-msg">{{ calibre_error }}</div>{% endif %}
        {% if calibre_success %}<div class="success-msg">{{ calibre_success }}</div>{% endif %}
        <form method="POST" action="/settings">
            <input type="hidden" name="form_type" value="calibre">
            <div class="form-group">
                <label>Sciezka do biblioteki Calibre</label>
                <input type="text" name="calibre_library_path" value="{{ calibre_settings.library_path }}" placeholder="/library">
            </div>
            <button type="submit" class="btn">Zapisz ustawienia Calibre</button>
        </form>
    </div>
</div></body></html>
"""

KINDLE_QUEUE_TEMPLATE = """
<!DOCTYPE html><html lang="pl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BookSearch — Kolejka Kindle</title>
<style>""" + SHARED_CSS + """
.queue-item { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 12px;
    padding: 16px; margin-bottom: 12px; }
.queue-item.pending { border-left: 3px solid #fdcb6e; }
.queue-item.sending { border-left: 3px solid #6c5ce7; }
.queue-item.sent { border-left: 3px solid #00b894; }
.queue-item.failed { border-left: 3px solid #d63031; }
.queue-item.found { border-left: 3px solid #0984e3; }
.item-title { font-size: 15px; font-weight: 600; color: #fff; margin-bottom: 4px; }
.item-author { font-size: 13px; color: #aaa; margin-bottom: 8px; }
.item-meta { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; font-size: 12px; color: #888; }
.status-badge { display: inline-block; padding: 2px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; }
.status-pending { background: rgba(253,203,110,0.15); color: #fdcb6e; }
.status-found { background: rgba(9,132,227,0.15); color: #74b9ff; }
.status-sending { background: rgba(108,92,231,0.15); color: #a29bfe; }
.status-sent { background: rgba(0,184,148,0.15); color: #00b894; }
.status-failed { background: rgba(214,48,49,0.15); color: #fab1a0; }
.item-actions { margin-top: 10px; display: flex; gap: 8px; }
.empty-state { text-align: center; padding: 60px 20px; color: #555; }
.section-title { color: #888; font-size: 12px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 1px; margin: 24px 0 12px; }
.btn-retry { padding: 6px 14px; border-radius: 8px; border: none;
    background: #fdcb6e; color: #000; font-size: 12px; cursor: pointer; font-weight: 600; }
.btn-retry:hover { background: #e6b84a; }
.btn-cancel { padding: 6px 14px; border-radius: 8px; border: none;
    background: #2d2d2d; color: #aaa; font-size: 12px; cursor: pointer; }
.btn-cancel:hover { background: #3d3d3d; }
</style></head><body>
<div class="container" style="max-width:700px;">
    <div class="topbar">
        <div class="topbar-user">{{ user }}</div>
        <div class="topbar-links">""" + TOPBAR_LINKS + """</div>
    </div>
    <h1><span>📱</span> Kolejka Kindle</h1>

    {% if not queue %}
    <div class="empty-state">
        <div style="font-size:48px; margin-bottom:16px;">📭</div>
        <div style="font-size:18px; color:#777;">Kolejka jest pusta</div>
        <div style="font-size:14px; color:#555; margin-top:8px;">Kliknij "📱 Kindle" przy wynikach wyszukiwania, aby dodac ksiazke</div>
    </div>
    {% else %}

    {% set pending_items = queue | selectattr('status', 'in', ['pending', 'found', 'sending']) | list %}
    {% set sent_items = queue | selectattr('status', 'equalto', 'sent') | list %}
    {% set failed_items = queue | selectattr('status', 'equalto', 'failed') | list %}

    {% if pending_items %}
    <div class="section-title">⏳ Oczekujace ({{ pending_items | length }})</div>
    {% for item in pending_items | reverse %}
    <div class="queue-item {{ item.status }}">
        <div class="item-title">{{ item.title }}</div>
        {% if item.author %}<div class="item-author">{{ item.author }}</div>{% endif %}
        <div class="item-meta">
            <span class="status-badge status-{{ item.status }}">
                {% if item.status == 'pending' %}⏳ Oczekuje{% elif item.status == 'found' %}🔍 Znaleziono{% elif item.status == 'sending' %}📤 Wysylam{% endif %}
            </span>
            <span>{{ item.format | upper }}</span>
            {% if item.target_format and item.target_format != item.format %}<span>➜ {{ item.target_format | upper }}</span>{% endif %}
            {% if item.added_at %}<span>Dodano: {{ item.added_at[:16].replace('T',' ') }}</span>{% endif %}
            {% if item.attempts > 0 %}<span>Proby: {{ item.attempts }}/3</span>{% endif %}
        </div>
        <div class="item-actions">
            <button class="btn-cancel" onclick="cancelItem('{{ item.md5 }}', this)">✕ Anuluj</button>
        </div>
    </div>
    {% endfor %}
    {% endif %}

    {% if failed_items %}
    <div class="section-title">❌ Nieudane ({{ failed_items | length }})</div>
    {% for item in failed_items | reverse %}
    <div class="queue-item failed">
        <div class="item-title">{{ item.title }}</div>
        {% if item.author %}<div class="item-author">{{ item.author }}</div>{% endif %}
        <div class="item-meta">
            <span class="status-badge status-failed">❌ Blad</span>
            <span>{{ item.format | upper }}</span>
            {% if item.error %}<span style="color:#fab1a0;">{{ item.error }}</span>{% endif %}
        </div>
        <div class="item-actions">
            <button class="btn-retry" onclick="retryItem('{{ item.md5 }}', this)">↺ Ponow</button>
            <button class="btn-cancel" onclick="cancelItem('{{ item.md5 }}', this)">✕ Usun</button>
        </div>
    </div>
    {% endfor %}
    {% endif %}

    {% if sent_items %}
    <div class="section-title">✅ Wyslane ({{ [sent_items | length, 20] | min }} z {{ sent_items | length }})</div>
    {% for item in sent_items | reverse | list | truncate_list(20) %}
    <div class="queue-item sent">
        <div class="item-title">{{ item.title }}</div>
        {% if item.author %}<div class="item-author">{{ item.author }}</div>{% endif %}
        <div class="item-meta">
            <span class="status-badge status-sent">✅ Wyslano</span>
            <span>{{ item.format | upper }}</span>
            {% if item.sent_at %}<span>Wyslano: {{ item.sent_at[:16].replace('T',' ') }}</span>{% endif %}
        </div>
    </div>
    {% endfor %}
    {% endif %}

    {% endif %}
</div>

<script>
async function cancelItem(md5, btn) {
    btn.disabled = true;
    btn.textContent = '...';
    const resp = await fetch('/api/kindle/queue/' + md5, {method: 'DELETE'});
    if (resp.ok) { location.reload(); }
    else { btn.disabled = false; btn.textContent = '✕ Anuluj'; alert('Blad usuwania'); }
}

async function retryItem(md5, btn) {
    btn.disabled = true;
    btn.textContent = '...';
    const resp = await fetch('/api/kindle/queue/' + md5 + '/retry', {method: 'POST'});
    if (resp.ok) { location.reload(); }
    else { btn.disabled = false; btn.textContent = '↺ Ponow'; alert('Blad ponawiania'); }
}

// Auto-refresh every 15s if there are pending items
const hasPending = document.querySelector('.queue-item.pending, .queue-item.sending, .queue-item.found');
if (hasPending) { setTimeout(() => location.reload(), 15000); }
</script>
</body></html>
"""

LOGS_TEMPLATE = """
<!DOCTYPE html><html lang="pl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BookSearch — Logi</title>
<style>""" + SHARED_CSS + """
.logs-controls { display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; align-items: center; }
.logs-controls input[type=text] { flex: 1; min-width: 180px; padding: 8px 14px; font-size: 14px; }
.logs-controls select { padding: 8px 12px; font-size: 14px; }
.log-entry { display: flex; gap: 12px; padding: 10px 14px; border-radius: 8px;
    margin-bottom: 6px; border: 1px solid transparent; font-size: 13px; }
.log-entry.download { background: rgba(108,92,231,0.08); border-color: rgba(108,92,231,0.2); }
.log-entry.kindle_queue { background: rgba(0,184,148,0.08); border-color: rgba(0,184,148,0.2); }
.log-entry.kindle_send { background: rgba(0,184,148,0.1); border-color: rgba(0,184,148,0.3); }
.log-entry.kindle_fail { background: rgba(214,48,49,0.08); border-color: rgba(214,48,49,0.2); }
.log-entry.conversion { background: rgba(253,203,110,0.08); border-color: rgba(253,203,110,0.2); }
.log-entry.conversion_fail { background: rgba(214,48,49,0.08); border-color: rgba(214,48,49,0.2); }
.log-entry.stacks_download { background: rgba(9,132,227,0.08); border-color: rgba(9,132,227,0.2); }
.log-entry.stacks_fail { background: rgba(214,48,49,0.08); border-color: rgba(214,48,49,0.2); }
.log-entry.error { background: rgba(214,48,49,0.1); border-color: rgba(214,48,49,0.3); }
.log-entry.import { background: rgba(108,92,231,0.08); border-color: rgba(108,92,231,0.2); }
.log-entry.auth_error { background: rgba(214,48,49,0.1); border-color: rgba(214,48,49,0.4); }
.log-entry.search_error { background: rgba(253,203,110,0.1); border-color: rgba(253,203,110,0.3); }
.log-entry.download_error { background: rgba(214,48,49,0.1); border-color: rgba(214,48,49,0.3); }
.log-entry.server_error { background: rgba(214,48,49,0.15); border-color: rgba(214,48,49,0.5); }
.log-icon { font-size: 16px; flex-shrink: 0; width: 20px; text-align: center; }
.log-body { flex: 1; min-width: 0; }
.log-title { font-weight: 600; color: #e0e0e0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.log-details { color: #aaa; font-size: 12px; margin-top: 2px; }
.log-meta { color: #666; font-size: 11px; margin-top: 2px; display: flex; gap: 10px; flex-wrap: wrap; }
.log-time { white-space: nowrap; }
.empty-state { text-align: center; padding: 60px 20px; color: #555; }
.section-live { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 12px;
    padding: 16px; margin-bottom: 20px; }
.section-live h3 { color: #e0e0e0; font-size: 14px; margin-bottom: 12px; }
.stacks-item { display: flex; gap: 10px; align-items: center; padding: 8px 0;
    border-bottom: 1px solid #222; font-size: 13px; }
.stacks-item:last-child { border-bottom: none; }
.stacks-progress { height: 4px; background: #2a2a2a; border-radius: 2px; margin-top: 4px; overflow: hidden; }
.stacks-progress-bar { height: 100%; background: #6c5ce7; border-radius: 2px; transition: width 0.3s; }
.badge-status { display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 11px; font-weight: 600; }
.badge-downloading { background: rgba(108,92,231,0.2); color: #a29bfe; }
.badge-queued { background: rgba(253,203,110,0.15); color: #fdcb6e; }
.badge-completed { background: rgba(0,184,148,0.15); color: #00b894; }
.badge-failed { background: rgba(214,48,49,0.15); color: #fab1a0; }
.load-more-btn { width: 100%; padding: 10px; background: #1a1a1a; border: 1px solid #2a2a2a;
    border-radius: 8px; color: #888; cursor: pointer; font-size: 13px; margin-top: 8px; }
.load-more-btn:hover { background: #222; color: #e0e0e0; }
.autorefresh-label { display: flex; align-items: center; gap: 8px; color: #888; font-size: 13px; cursor: pointer; }
.clear-logs-btn { padding: 8px 16px; border-radius: 8px; border: none; background: #d63031;
    color: #fff; font-size: 13px; cursor: pointer; font-weight: 600; }
.clear-logs-btn:hover { background: #c0392b; }
</style></head><body>
<div class="container" style="max-width: 900px;">
    <div class="topbar">
        <div class="topbar-user">{{ user }}</div>
        <div class="topbar-links">""" + TOPBAR_LINKS + """</div>
    </div>
    <h1><span>📋</span> Logi aktywności</h1>

    <!-- Live Stacks status section -->
    <div class="section-live" id="stacks-section">
        <h3>📡 Status Stacks <span id="stacks-refresh-info" style="color:#555; font-size:11px; font-weight:400;"></span></h3>
        <div id="stacks-content"><span style="color:#555; font-size:12px;">Ładowanie...</span></div>
    </div>

    <div class="logs-controls">
        <input type="text" id="log-search" placeholder="Szukaj w logach..." oninput="filterLogs()">
        <select id="log-type" onchange="filterLogs()">
            <option value="">Wszystkie typy</option>
            <option value="download">📥 Pobieranie</option>
            <option value="kindle_queue">📱 Kolejka Kindle</option>
            <option value="kindle_send">✅ Wysłano Kindle</option>
            <option value="kindle_fail">❌ Błąd Kindle</option>
            <option value="conversion">🔄 Konwersja</option>
            <option value="conversion_fail">⚠️ Błąd konwersji</option>
            <option value="stacks_download">📦 Pobranie Stacks</option>
            <option value="stacks_fail">💥 Błąd Stacks</option>
            <option value="auth_error">🔐 Błąd autoryzacji</option>
            <option value="search_error">🔍 Błąd wyszukiwania</option>
            <option value="download_error">📥 Błąd pobierania</option>
            <option value="server_error">💥 Błąd serwera</option>
            <option value="error">⚠️ Błąd ogólny</option>
        </select>
        <label class="autorefresh-label">
            <input type="checkbox" id="autorefresh" onchange="toggleAutoRefresh()">
            Auto-odświeżanie
        </label>
        <button class="clear-logs-btn" onclick="clearLogs()">🗑️ Wyczyść logi</button>
    </div>

    <div id="log-count" style="color:#666; font-size:12px; margin-bottom:10px;"></div>
    <div id="log-list"></div>
    <button class="load-more-btn" id="load-more-btn" onclick="loadMore()" style="display:none;">
        Załaduj więcej...
    </button>
</div>

<script>
let allLogs = [];
let displayedCount = 100;
let autoRefreshTimer = null;

const TYPE_ICONS = {
    download: '📥',
    kindle_queue: '📱',
    kindle_send: '✅',
    kindle_fail: '❌',
    conversion: '🔄',
    conversion_fail: '⚠️',
    stacks_download: '📦',
    stacks_fail: '💥',
    error: '⚠️',
    import: '📂',
    auth_error: '🔐',
    search_error: '🔍',
    download_error: '📥',
    server_error: '💥',
};

function getIcon(type) {
    return TYPE_ICONS[type] || '📝';
}

async function loadLogs() {
    try {
        const resp = await fetch('/api/logs?limit=500');
        if (resp.status === 401) { location.href = '/login'; return; }
        const data = await resp.json();
        allLogs = (data.logs || []).reverse(); // newest first
        filterLogs();
    } catch(e) {
        document.getElementById('log-list').innerHTML =
            '<div class="empty-state">Błąd ładowania logów: ' + esc(e.message) + '</div>';
    }
}

function filterLogs() {
    const q = document.getElementById('log-search').value.toLowerCase().trim();
    const typeFilter = document.getElementById('log-type').value;
    let filtered = allLogs.filter(entry => {
        if (typeFilter && entry.type !== typeFilter) return false;
        if (q) {
            const hay = [entry.title, entry.author, entry.details, entry.user].join(' ').toLowerCase();
            if (!hay.includes(q)) return false;
        }
        return true;
    });
    renderLogs(filtered);
}

function renderLogs(logs) {
    const container = document.getElementById('log-list');
    const countEl = document.getElementById('log-count');
    const loadMoreBtn = document.getElementById('load-more-btn');

    const visible = logs.slice(0, displayedCount);
    countEl.textContent = 'Wyświetlono: ' + visible.length + ' z ' + logs.length + ' wpisów';

    if (logs.length === 0) {
        container.innerHTML = '<div class="empty-state"><div style="font-size:40px;">📭</div><div style="color:#777; margin-top:12px;">Brak logów</div></div>';
        loadMoreBtn.style.display = 'none';
        return;
    }

    container.innerHTML = visible.map(entry => {
        const icon = getIcon(entry.type);
        const ts = entry.timestamp ? entry.timestamp.replace('T', ' ') : '';
        return `<div class="log-entry ${esc(entry.type)}">
            <div class="log-icon">${icon}</div>
            <div class="log-body">
                <div class="log-title">${esc(entry.title || entry.details)}</div>
                ${entry.title && entry.details ? '<div class="log-details">' + esc(entry.details) + '</div>' : ''}
                <div class="log-meta">
                    <span class="log-time">${esc(ts)}</span>
                    ${entry.author ? '<span>' + esc(entry.author) + '</span>' : ''}
                    ${entry.user ? '<span>👤 ' + esc(entry.user) + '</span>' : ''}
                    ${entry.md5 ? '<span style="font-family:monospace; color:#444;">' + esc(entry.md5.substring(0,8)) + '...</span>' : ''}
                </div>
            </div>
        </div>`;
    }).join('');

    if (logs.length > displayedCount) {
        loadMoreBtn.style.display = 'block';
        loadMoreBtn.textContent = 'Załaduj więcej (' + (logs.length - displayedCount) + ' pozostało)...';
    } else {
        loadMoreBtn.style.display = 'none';
    }
}

function loadMore() {
    displayedCount += 100;
    filterLogs();
}

function toggleAutoRefresh() {
    const enabled = document.getElementById('autorefresh').checked;
    if (enabled) {
        autoRefreshTimer = setInterval(() => { displayedCount = 100; loadLogs(); }, 10000);
    } else {
        clearInterval(autoRefreshTimer);
        autoRefreshTimer = null;
    }
}

async function clearLogs() {
    if (!confirm('Czy na pewno chcesz wyczyścić wszystkie logi?')) return;
    const resp = await fetch('/api/logs', {method: 'DELETE'});
    if (resp.ok) { allLogs = []; filterLogs(); }
    else { alert('Błąd czyszczenia logów'); }
}

// --- Stacks live status ---
async function loadStacksStatus() {
    try {
        const resp = await fetch('/api/stacks/status');
        if (!resp.ok) { throw new Error('HTTP ' + resp.status); }
        const data = await resp.json();
        renderStacksStatus(data);
        document.getElementById('stacks-refresh-info').textContent = '(odświeżono ' + new Date().toLocaleTimeString('pl') + ')';
    } catch(e) {
        document.getElementById('stacks-content').innerHTML =
            '<span style="color:#555; font-size:12px;">Stacks niedostępny: ' + esc(e.message) + '</span>';
    }
}

function renderStacksStatus(data) {
    const el = document.getElementById('stacks-content');
    const current = data.current_downloads || [];
    const queue = data.queue || [];
    let html = '';

    if (current.length === 0 && queue.length === 0) {
        el.innerHTML = '<span style="color:#555; font-size:12px;">Brak aktywnych pobierań</span>';
        return;
    }

    if (current.length > 0) {
        html += '<div style="color:#888; font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:8px;">📥 Aktywne pobieranie (' + current.length + ')</div>';
        for (const item of current) {
            const progress = item.progress;
            let pct = 0;
            let speed = '';
            if (progress && typeof progress === 'object') {
                pct = progress.percent || 0;
                speed = progress.speed ? ' · ' + progress.speed : '';
            } else if (typeof progress === 'number') {
                pct = progress;
            }
            html += `<div class="stacks-item">
                <div style="flex:1; min-width:0;">
                    <div style="color:#e0e0e0; font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${esc(item.title || item.filename || item.md5 || '?')}</div>
                    <div class="stacks-progress"><div class="stacks-progress-bar" style="width:${pct}%"></div></div>
                    <div style="color:#666; font-size:11px; margin-top:2px;">${pct}%${speed}</div>
                </div>
                <span class="badge-status badge-downloading">⬇️ Pobieranie</span>
            </div>`;
        }
    }

    if (queue.length > 0) {
        html += '<div style="color:#888; font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; margin: 12px 0 8px;">⏳ W kolejce Stacks (' + queue.length + ')</div>';
        for (const item of queue.slice(0, 5)) {
            html += `<div class="stacks-item">
                <div style="flex:1; min-width:0; color:#aaa; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${esc(item.title || item.filename || item.md5 || '?')}</div>
                <span class="badge-status badge-queued">⏳ Oczekuje</span>
            </div>`;
        }
        if (queue.length > 5) {
            html += '<div style="color:#555; font-size:11px; padding:4px 0;">+ ' + (queue.length - 5) + ' więcej w kolejce</div>';
        }
    }

    el.innerHTML = html;
}

function esc(s) { return s ? String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') : ''; }

// Init
loadLogs();
loadStacksStatus();
setInterval(loadStacksStatus, 5000); // Stacks status every 5s
</script>
</body></html>
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
.calibre-badge { display: inline-block; padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 600; }
.calibre-exact { background: rgba(0,184,148,0.2); color: #00b894; }
.calibre-title { background: rgba(108,92,231,0.15); color: #a29bfe; }
.calibre-author { background: rgba(253,203,110,0.15); color: #fdcb6e; }
.footer { text-align: center; padding: 30px; color: #555; font-size: 12px; }
.selection-panel {
    position: fixed;
    right: -320px;
    top: 80px;
    width: 300px;
    max-height: calc(100vh - 120px);
    background: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-radius: 12px 0 0 12px;
    box-shadow: -4px 0 16px rgba(0,0,0,0.3);
    z-index: 160;
    display: flex;
    flex-direction: column;
    transition: right 0.3s ease;
}
.selection-panel.visible { right: 0; }
.selection-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 14px 16px;
    border-bottom: 1px solid #2a2a2a;
    flex-shrink: 0;
}
.selection-title { color: #fff; font-weight: 600; font-size: 14px; }
.selection-clear { background: none; border: none; color: #888; font-size: 18px; cursor: pointer; padding: 0 4px; line-height: 1; }
.selection-clear:hover { color: #d63031; }
.selection-list { flex: 1; overflow-y: auto; padding: 8px 0; }
.selection-item {
    padding: 8px 16px;
    border-bottom: 1px solid #222;
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 8px;
}
.selection-item:last-child { border-bottom: none; }
.selection-item-info { flex: 1; min-width: 0; }
.selection-item-title { color: #e0e0e0; font-size: 13px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.selection-item-author { color: #888; font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.selection-item-remove { background: none; border: none; color: #666; font-size: 14px; cursor: pointer; padding: 0; flex-shrink: 0; line-height: 1; }
.selection-item-remove:hover { color: #d63031; }
.selection-actions {
    padding: 12px 16px;
    border-top: 1px solid #2a2a2a;
    display: flex;
    gap: 8px;
    flex-direction: column;
    flex-shrink: 0;
}
.kindle-fmt-row { display: flex; align-items: center; gap: 8px; }
.kindle-fmt-label { color: #888; font-size: 12px; white-space: nowrap; }
.kindle-fmt-select { flex: 1; padding: 6px 10px; font-size: 12px; border-radius: 6px;
    border: 1px solid #333; background: #1a1a1a; color: #e0e0e0; }
.selection-actions .bulk-btn-calibre,
.selection-actions .bulk-btn-kindle { flex: 1; padding: 10px 12px; font-size: 13px; }
.selection-action-row { display: flex; gap: 8px; }
@media (max-width: 768px) {
    .selection-panel { width: 260px; }
    .container { padding-right: 10px; }
}
</style></head><body>
<div class="container">
    <div class="topbar">
        <div class="topbar-user">{{ user }}</div>
        <div class="topbar-links">""" + TOPBAR_LINKS + """</div>
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
        Szukanie trwa 10-30s (Cloudflare challenge) &bull; Wyniki z wielu stron
    </div>
</div>

<div class="bulk-bar" id="bulk-bar">
    <span class="bulk-count" id="bulk-count">Zaznaczono: 0</span>
    <button class="bulk-btn-calibre" id="bulk-calibre" onclick="bulkDownload(false)">📚 Calibre All</button>
    <button class="bulk-btn-kindle" id="bulk-kindle" onclick="bulkDownload(true)">📱 Kindle All</button>
</div>

<div class="selection-panel" id="selection-panel">
    <div class="selection-header">
        <span class="selection-title">📚 Zaznaczone (<span id="selection-count">0</span>)</span>
        <button class="selection-clear" onclick="clearSelection()" title="Wyczysc zaznaczenie">✕</button>
    </div>
    <div class="selection-list" id="selection-list"></div>
    <div class="selection-actions">
        <div class="kindle-fmt-row">
            <span class="kindle-fmt-label">Format Kindle:</span>
            <select class="kindle-fmt-select" id="kindle-target-fmt">
                <option value="epub">EPUB</option>
                <option value="mobi">MOBI</option>
                <option value="azw3">AZW3</option>
                <option value="pdf">PDF</option>
            </select>
        </div>
        <div class="selection-action-row">
            <button class="bulk-btn-calibre" onclick="bulkDownload(false)">📚 Calibre</button>
            <button class="bulk-btn-kindle" onclick="bulkDownload(true)">📱 Kindle</button>
        </div>
    </div>
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
    updateSelectionPanel();
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
            results.innerHTML = '<div style="text-align:center;color:#888;font-size:13px;margin-bottom:12px;">Znaleziono: ' + data.length + ' wynikow</div>' + data.map((r, i) => `
                <div class="result" id="result-${i}">
                    <input type="checkbox" class="result-checkbox" data-idx="${i}" onchange="toggleSelect(${i}, this.checked)">
                    <div class="result-body">
                        <div class="result-title">${esc(r.title)} ${r.calibre_status === 'exact' ? '<span class="calibre-badge calibre-exact">📖 Juz w Calibre</span>' : r.calibre_status === 'title' ? '<span class="calibre-badge calibre-title">📗 Tytul w bibliotece</span>' : ''}</div>
                        ${r.author ? '<div class="result-author">' + esc(r.author) + (r.calibre_status === 'author' ? ' <span class="calibre-badge calibre-author">✍️ Autor w bibliotece</span>' : '') + '</div>' : ''}
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
    const r = searchResults[idx] || {};
    const targetFmt = sendToKindle ? (document.getElementById('kindle-target-fmt')?.value || 'epub') : (r.format || 'epub');
    btn.disabled = true; btn.textContent = sendToKindle ? 'Wysylam...' : 'Pobieram...';
    try {
        const resp = await fetch('/api/download', {method:'POST', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({md5, send_to_kindle: sendToKindle, title: r.title || '', author: r.author || '', format: r.format || 'epub', target_format: targetFmt})});
        if (resp.status === 401) { location.href = '/login'; return; }
        const data = await resp.json();
        if (data.success) {
            btn.textContent = sendToKindle ? '📱 W kolejce!' : '📚 W kolejce!';
            btn.className = (sendToKindle ? 'btn-kindle' : 'btn-calibre') + ' done';
            showToast(sendToKindle ? 'Pobrano + dodano do kolejki Kindle' : 'Pobrano do Calibre');
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
    updateSelectionPanel();
}

function getSelectedItems() {
    const checkboxes = document.querySelectorAll('.result-checkbox:checked');
    return Array.from(checkboxes).map(cb => {
        const idx = parseInt(cb.dataset.idx);
        return { md5: searchResults[idx].md5, title: searchResults[idx].title, author: searchResults[idx].author || '', format: searchResults[idx].format || 'epub', idx };
    });
}

function updateSelectionPanel() {
    const selected = getSelectedItems();
    const panel = document.getElementById('selection-panel');
    const list = document.getElementById('selection-list');
    const count = document.getElementById('selection-count');
    const bulkBar = document.getElementById('bulk-bar');
    const bulkCount = document.getElementById('bulk-count');
    count.textContent = selected.length;
    if (selected.length > 0) {
        panel.classList.add('visible');
        if (bulkBar) { bulkBar.classList.add('visible'); bulkCount.textContent = 'Zaznaczono: ' + selected.length; }
        list.innerHTML = selected.map(item => `
            <div class="selection-item">
                <div class="selection-item-info">
                    <div class="selection-item-title">${esc(item.title)}</div>
                    <div class="selection-item-author">${esc(searchResults[item.idx].author || '')}</div>
                </div>
                <button class="selection-item-remove" onclick="removeSelection(${item.idx})" title="Usun">✕</button>
            </div>
        `).join('');
    } else {
        panel.classList.remove('visible');
        if (bulkBar) { bulkBar.classList.remove('visible'); }
        list.innerHTML = '';
    }
}

function clearSelection() {
    document.querySelectorAll('.result-checkbox:checked').forEach(cb => {
        cb.checked = false;
        const idx = parseInt(cb.dataset.idx);
        const el = document.getElementById('result-' + idx);
        if (el) el.classList.remove('selected');
    });
    updateSelectionPanel();
}

function removeSelection(idx) {
    const cb = document.querySelector('.result-checkbox[data-idx="' + idx + '"]');
    if (cb) {
        cb.checked = false;
        const el = document.getElementById('result-' + idx);
        if (el) el.classList.remove('selected');
    }
    updateSelectionPanel();
}

async function bulkDownload(sendToKindle) {
    const items = getSelectedItems();
    if (items.length === 0) return;
    const btnId = sendToKindle ? 'bulk-kindle' : 'bulk-calibre';
    const btn = document.getElementById(btnId);
    btn.disabled = true;
    const label = sendToKindle ? '📱 Kindle All' : '📚 Calibre All';
    btn.textContent = '0/' + items.length + '...';
    const targetFmt = sendToKindle ? (document.getElementById('kindle-target-fmt')?.value || 'epub') : null;
    try {
        const payload = items.map(it => ({
            md5: it.md5, send_to_kindle: sendToKindle,
            title: it.title, author: it.author, format: it.format,
            target_format: targetFmt || it.format
        }));
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
                if (sendToKindle && kinBtn) { kinBtn.textContent = '📱 W kolejce!'; kinBtn.className = 'btn-kindle done'; kinBtn.disabled = true; }
                if (!sendToKindle && calBtn) { calBtn.textContent = '📚 W kolejce!'; calBtn.className = 'btn-calibre done'; calBtn.disabled = true; }
            }
        });
    } catch(e) { showToast(e.message, true); }
    btn.disabled = false; btn.textContent = label;
}

function showToast(msg,isError) { const t=document.getElementById('toast'); t.textContent=msg; t.className='toast'+(isError?' error':''); t.style.display='block'; setTimeout(()=>t.style.display='none',4000); }
function esc(s) { return s ? String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') : ''; }
</script></body></html>
"""


LIBRARY_TEMPLATE = """
<!DOCTYPE html><html lang="pl"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BookSearch — Biblioteka</title>
<style>""" + SHARED_CSS + """
.library-controls { display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap; }
.library-controls input[type=text] { flex: 1; min-width: 200px; }
.library-controls select { padding: 8px 12px; border-radius: 8px; border: 1px solid #333;
    background: #1a1a1a; color: #e0e0e0; font-size: 14px; }
.library-stats { color: #888; font-size: 13px; margin-bottom: 12px; text-align: center; }
.lib-table { width: 100%; border-collapse: collapse; }
.lib-table th { padding: 10px 12px; text-align: left; color: #888; font-size: 12px;
    font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;
    border-bottom: 1px solid #2a2a2a; cursor: pointer; user-select: none; white-space: nowrap; }
.lib-table th:hover { color: #a29bfe; }
.lib-table th .sort-arrow { margin-left: 4px; opacity: 0.4; }
.lib-table th.sorted .sort-arrow { opacity: 1; color: #6c5ce7; }
.lib-table td { padding: 10px 12px; border-bottom: 1px solid #1e1e1e; font-size: 14px; vertical-align: middle; }
.lib-table tr:hover td { background: #1e1e1e; }
.lib-table tr.row-selected td { background: #1e1a2e; }
.lib-row-checkbox { width: 16px; height: 16px; accent-color: #6c5ce7; cursor: pointer; }
.lib-title { color: #e0e0e0; font-weight: 500; }
.lib-author { color: #aaa; }
.lib-formats { display: flex; gap: 4px; flex-wrap: wrap; }
.fmt-select { padding: 3px 8px; border-radius: 5px; border: none; background: #2a2a2a;
    color: #e0e0e0; font-size: 11px; cursor: pointer; font-weight: 600; text-transform: uppercase; }
.fmt-select option { background: #1a1a1a; }
.lib-size { color: #666; font-size: 12px; white-space: nowrap; }
.lib-date { color: #666; font-size: 12px; white-space: nowrap; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 6px;
    font-size: 11px; font-weight: 600; text-transform: uppercase; }
.tag-epub { background: rgba(108,92,231,0.2); color: #a29bfe; }
.tag-pdf { background: rgba(214,48,49,0.2); color: #fab1a0; }
.tag-mobi { background: rgba(0,206,209,0.2); color: #81ecec; }
.tag-azw3 { background: rgba(0,206,209,0.2); color: #81ecec; }
.tag-other { background: rgba(100,100,100,0.2); color: #aaa; }
.spinner { display: inline-block; width: 24px; height: 24px;
    border: 3px solid #333; border-top-color: #6c5ce7;
    border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.status { text-align: center; padding: 40px; color: #888; }
.toast { position: fixed; bottom: 20px; right: 20px; padding: 12px 20px;
    border-radius: 10px; background: #00b894; color: #fff; font-weight: 600;
    font-size: 14px; display: none; z-index: 200; box-shadow: 0 4px 12px rgba(0,0,0,0.3); }
.toast.error { background: #d63031; }
.bulk-bar { position: fixed; bottom: 0; left: 0; right: 0; background: #1a1a1a;
    border-top: 1px solid #333; padding: 12px 20px; display: none; z-index: 150;
    justify-content: center; align-items: center; gap: 16px; box-shadow: 0 -4px 16px rgba(0,0,0,0.5); }
.bulk-bar.visible { display: flex; }
.bulk-count { color: #e0e0e0; font-size: 14px; font-weight: 600; }
.bulk-btn-zip { padding: 10px 20px; border-radius: 8px; border: none;
    background: #6c5ce7; color: #fff; font-size: 14px; cursor: pointer; font-weight: 600; }
.bulk-btn-zip:hover { background: #5a4bd1; }
.bulk-btn-zip:disabled { opacity: 0.5; cursor: wait; }
.bulk-btn-kindle { padding: 10px 20px; border-radius: 8px; border: none;
    background: #00b894; color: #fff; font-size: 14px; cursor: pointer; font-weight: 600; }
.bulk-btn-kindle:hover { background: #00a381; }
.bulk-btn-kindle:disabled { opacity: 0.5; cursor: wait; }
.selection-panel {
    position: fixed; right: -320px; top: 80px; width: 300px;
    max-height: calc(100vh - 120px); background: #1a1a1a;
    border: 1px solid #2a2a2a; border-radius: 12px 0 0 12px;
    box-shadow: -4px 0 16px rgba(0,0,0,0.3); z-index: 160;
    display: flex; flex-direction: column; transition: right 0.3s ease; }
.selection-panel.visible { right: 0; }
.selection-header { display: flex; justify-content: space-between; align-items: center;
    padding: 14px 16px; border-bottom: 1px solid #2a2a2a; flex-shrink: 0; }
.selection-title { color: #fff; font-weight: 600; font-size: 14px; }
.selection-clear { background: none; border: none; color: #888; font-size: 18px; cursor: pointer; padding: 0 4px; line-height: 1; }
.selection-clear:hover { color: #d63031; }
.selection-list { flex: 1; overflow-y: auto; padding: 8px 0; }
.selection-item { padding: 8px 16px; border-bottom: 1px solid #222;
    display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
.selection-item:last-child { border-bottom: none; }
.selection-item-info { flex: 1; min-width: 0; }
.selection-item-title { color: #e0e0e0; font-size: 13px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.selection-item-author { color: #888; font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.selection-item-remove { background: none; border: none; color: #666; font-size: 14px; cursor: pointer; padding: 0; flex-shrink: 0; line-height: 1; }
.selection-item-remove:hover { color: #d63031; }
.selection-actions { padding: 12px 16px; border-top: 1px solid #2a2a2a;
    display: flex; flex-direction: column; gap: 8px; flex-shrink: 0; }
.kindle-fmt-row { display: flex; align-items: center; gap: 8px; }
.kindle-fmt-label { color: #888; font-size: 12px; white-space: nowrap; }
.kindle-fmt-select { flex: 1; padding: 6px 10px; font-size: 12px; border-radius: 6px;
    border: 1px solid #333; background: #1a1a1a; color: #e0e0e0; }
.selection-action-row { display: flex; gap: 8px; }
.selection-actions .bulk-btn-zip,
.selection-actions .bulk-btn-kindle { flex: 1; padding: 10px 12px; font-size: 13px; }
.lib-container { padding-bottom: 80px; overflow-x: auto; }
@media (max-width: 768px) {
    .selection-panel { width: 260px; }
    .lib-table th, .lib-table td { padding: 8px 6px; }
}
</style></head><body>
<div class="container" style="max-width: 1100px;">
    <div class="topbar">
        <div class="topbar-user">{{ user }}</div>
        <div class="topbar-links">""" + TOPBAR_LINKS + """</div>
    </div>

    <h1><span>📚</span> Biblioteka</h1>

    <div class="library-controls">
        <input type="text" id="lib-filter" placeholder="Filtruj po tytule lub autorze..." oninput="applyFilters()">
        <select id="fmt-filter" onchange="applyFilters()">
            <option value="">Wszystkie formaty</option>
            <option value="EPUB">EPUB</option>
            <option value="PDF">PDF</option>
            <option value="MOBI">MOBI</option>
            <option value="AZW3">AZW3</option>
            <option value="FB2">FB2</option>
        </select>
        <button class="btn btn-sm" onclick="selectAll()" style="width:auto;">Zaznacz wszystkie</button>
        <button class="btn btn-sm" onclick="clearSelection()" style="width:auto; background:#333;">Wyczysc</button>
    </div>

    <div class="library-stats" id="lib-stats">Ladowanie...</div>

    <div class="lib-container">
        <div id="lib-content">
            <div class="status"><div class="spinner"></div><br><br>Ladowanie biblioteki...</div>
        </div>
    </div>
</div>

<div class="bulk-bar" id="bulk-bar">
    <span class="bulk-count" id="bulk-count">Zaznaczono: 0</span>
    <button class="bulk-btn-zip" id="bulk-zip" onclick="bulkDownloadZip()">📥 Pobierz ZIP</button>
    <button class="bulk-btn-kindle" id="bulk-kindle" onclick="bulkSendKindle()">📱 Kindle</button>
</div>

<div class="selection-panel" id="selection-panel">
    <div class="selection-header">
        <span class="selection-title">📚 Zaznaczone (<span id="selection-count">0</span>)</span>
        <button class="selection-clear" onclick="clearSelection()" title="Wyczysc zaznaczenie">✕</button>
    </div>
    <div class="selection-list" id="selection-list"></div>
    <div class="selection-actions">
        <div class="kindle-fmt-row">
            <span class="kindle-fmt-label">Format Kindle:</span>
            <select class="kindle-fmt-select" id="kindle-target-fmt">
                <option value="epub">EPUB</option>
                <option value="mobi">MOBI</option>
                <option value="azw3">AZW3</option>
                <option value="pdf">PDF</option>
            </select>
        </div>
        <div class="selection-action-row">
            <button class="bulk-btn-zip" onclick="bulkDownloadZip()">📥 ZIP</button>
            <button class="bulk-btn-kindle" onclick="bulkSendKindle()">📱 Kindle</button>
        </div>
    </div>
</div>

<div class="toast" id="toast"></div>

<script>
let allBooks = [];
let filteredBooks = [];
let selectedBooks = new Map(); // id -> {book, format}
let sortCol = 'added';
let sortDir = -1; // -1 = desc, 1 = asc

const FORMAT_PREF = ['EPUB', 'MOBI', 'AZW3', 'PDF', 'FB2'];

function pickFormat(formats) {
    for (const pref of FORMAT_PREF) {
        const f = formats.find(f => f.format === pref);
        if (f) return f.format;
    }
    return formats[0] ? formats[0].format : '';
}

function fmtSize(bytes) {
    if (!bytes) return '';
    if (bytes > 1048576) return (bytes / 1048576).toFixed(1) + ' MB';
    return Math.round(bytes / 1024) + ' KB';
}

function renderTable(books) {
    if (books.length === 0) {
        return '<div class="status">Brak ksiazek pasujacych do filtrow</div>';
    }

    const th = (col, label) => {
        const isSorted = sortCol === col;
        const arrow = sortDir === -1 ? '▼' : '▲';
        return `<th onclick="sortBy('${col}')" class="${isSorted ? 'sorted' : ''}">
            ${label}<span class="sort-arrow">${isSorted ? arrow : '↕'}</span>
        </th>`;
    };

    let rows = '';
    const LIMIT = 1000;
    const visible = books.slice(0, LIMIT);
    for (const b of visible) {
        const isSelected = selectedBooks.has(b.id);
        const selFmt = isSelected ? selectedBooks.get(b.id).format : pickFormat(b.formats);
        const totalSize = b.formats.reduce((s, f) => s + (f.size || 0), 0);
        rows += `<tr class="${isSelected ? 'row-selected' : ''}" id="row-${b.id}">
            <td><input type="checkbox" class="lib-row-checkbox" data-id="${b.id}"
                ${isSelected ? 'checked' : ''} onchange="toggleBook(${b.id}, this.checked)"></td>
            <td class="lib-title">${esc(b.title)}</td>
            <td class="lib-author">${esc(b.author)}</td>
            <td>
                <select class="fmt-select" id="fmt-${b.id}" onchange="changeFmt(${b.id}, this.value)">
                    ${b.formats.map(f => `<option value="${esc(f.format)}" ${f.format === selFmt ? 'selected' : ''}>${esc(f.format)}</option>`).join('')}
                </select>
            </td>
            <td class="lib-size">${fmtSize(totalSize)}</td>
            <td class="lib-date">${esc(b.added)}</td>
        </tr>`;
    }

    let extraNote = '';
    if (books.length > LIMIT) {
        extraNote = `<tr><td colspan="6" style="text-align:center;color:#888;padding:12px;">
            Wyswietlono ${LIMIT} z ${books.length} ksiazek. Uzyj filtru aby zawezic wyniki.
        </td></tr>`;
    }

    return `<table class="lib-table">
        <thead><tr>
            <th style="width:32px;"><input type="checkbox" id="check-all" class="lib-row-checkbox" onchange="toggleAllVisible(this.checked)" title="Zaznacz wszystkie widoczne"></th>
            ${th('title','Tytuł')}
            ${th('author','Autor')}
            <th>Format</th>
            ${th('size','Rozmiar')}
            ${th('added','Dodano')}
        </tr></thead>
        <tbody>${rows}${extraNote}</tbody>
    </table>`;
}

function applyFilters() {
    const q = document.getElementById('lib-filter').value.toLowerCase().trim();
    const fmt = document.getElementById('fmt-filter').value.toUpperCase();
    filteredBooks = allBooks.filter(b => {
        if (q && !b.title.toLowerCase().includes(q) && !b.author.toLowerCase().includes(q)) return false;
        if (fmt && !b.formats.some(f => f.format === fmt)) return false;
        return true;
    });
    sortBooks();
    document.getElementById('lib-content').innerHTML = renderTable(filteredBooks);
    updateStats();
}

function sortBooks() {
    filteredBooks.sort((a, b) => {
        let va, vb;
        if (sortCol === 'title') { va = a.title.toLowerCase(); vb = b.title.toLowerCase(); }
        else if (sortCol === 'author') { va = a.author.toLowerCase(); vb = b.author.toLowerCase(); }
        else if (sortCol === 'size') {
            va = a.formats.reduce((s, f) => s + (f.size || 0), 0);
            vb = b.formats.reduce((s, f) => s + (f.size || 0), 0);
        }
        else { va = a.added || ''; vb = b.added || ''; }
        if (va < vb) return -1 * sortDir;
        if (va > vb) return 1 * sortDir;
        return 0;
    });
}

function sortBy(col) {
    if (sortCol === col) { sortDir *= -1; } else { sortCol = col; sortDir = 1; }
    sortBooks();
    document.getElementById('lib-content').innerHTML = renderTable(filteredBooks);
    updateStats();
}

function toggleBook(id, checked) {
    const book = allBooks.find(b => b.id === id);
    if (!book) return;
    if (checked) {
        const fmt = document.getElementById('fmt-' + id)?.value || pickFormat(book.formats);
        selectedBooks.set(id, {book, format: fmt});
        const row = document.getElementById('row-' + id);
        if (row) row.className = 'row-selected';
    } else {
        selectedBooks.delete(id);
        const row = document.getElementById('row-' + id);
        if (row) row.className = '';
    }
    updateSelectionPanel();
}

function changeFmt(id, fmt) {
    if (selectedBooks.has(id)) {
        selectedBooks.get(id).format = fmt;
        updateSelectionPanel();
    }
}

function toggleAllVisible(checked) {
    const LIMIT = 1000;
    const visible = filteredBooks.slice(0, LIMIT);
    for (const b of visible) {
        const cb = document.querySelector('.lib-row-checkbox[data-id="' + b.id + '"]');
        if (cb) cb.checked = checked;
        if (checked) {
            const fmt = document.getElementById('fmt-' + b.id)?.value || pickFormat(b.formats);
            selectedBooks.set(b.id, {book: b, format: fmt});
            const row = document.getElementById('row-' + b.id);
            if (row) row.className = 'row-selected';
        } else {
            selectedBooks.delete(b.id);
            const row = document.getElementById('row-' + b.id);
            if (row) row.className = '';
        }
    }
    updateSelectionPanel();
}

function selectAll() {
    toggleAllVisible(true);
    const ca = document.getElementById('check-all');
    if (ca) ca.checked = true;
}

function clearSelection() {
    selectedBooks.clear();
    document.querySelectorAll('.lib-row-checkbox').forEach(cb => cb.checked = false);
    document.querySelectorAll('.row-selected').forEach(row => row.className = '');
    const ca = document.getElementById('check-all');
    if (ca) ca.checked = false;
    updateSelectionPanel();
}

function updateSelectionPanel() {
    const count = selectedBooks.size;
    const panel = document.getElementById('selection-panel');
    const list = document.getElementById('selection-list');
    const countEl = document.getElementById('selection-count');
    const bulkBar = document.getElementById('bulk-bar');
    const bulkCount = document.getElementById('bulk-count');

    countEl.textContent = count;
    bulkCount.textContent = 'Zaznaczono: ' + count;

    if (count > 0) {
        panel.classList.add('visible');
        bulkBar.classList.add('visible');
        const items = Array.from(selectedBooks.entries());
        list.innerHTML = items.map(([id, {book, format}]) => `
            <div class="selection-item">
                <div class="selection-item-info">
                    <div class="selection-item-title">${esc(book.title)}</div>
                    <div class="selection-item-author">${esc(book.author)} · ${esc(format)}</div>
                </div>
                <button class="selection-item-remove" onclick="removeSelection(${id})" title="Usun">✕</button>
            </div>
        `).join('');
    } else {
        panel.classList.remove('visible');
        bulkBar.classList.remove('visible');
        list.innerHTML = '';
    }
}

function removeSelection(id) {
    selectedBooks.delete(id);
    const cb = document.querySelector('.lib-row-checkbox[data-id="' + id + '"]');
    if (cb) cb.checked = false;
    const row = document.getElementById('row-' + id);
    if (row) row.className = '';
    updateSelectionPanel();
}

function updateStats() {
    const total = allBooks.length;
    const filtered = filteredBooks.length;
    const el = document.getElementById('lib-stats');
    if (total === filtered) {
        el.textContent = 'Łącznie: ' + total + ' książek';
    } else {
        el.textContent = 'Wyświetlono: ' + filtered + ' z ' + total + ' książek';
    }
}

async function bulkDownloadZip() {
    if (selectedBooks.size === 0) return;
    const btn = document.getElementById('bulk-zip');
    btn.disabled = true;
    btn.textContent = 'Przygotowuję...';
    const items = Array.from(selectedBooks.entries()).map(([id, {book, format}]) => ({
        id, format
    }));
    try {
        const resp = await fetch('/api/library/download', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({items})
        });
        if (resp.status === 401) { location.href = '/login'; return; }
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            showToast(data.error || 'Błąd pobierania ZIP', true);
            return;
        }
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        const today = new Date().toISOString().split('T')[0];
        a.download = 'booksearch-export-' + today + '.zip';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast('ZIP gotowy! (' + items.length + ' książek)');
    } catch(e) {
        showToast(e.message, true);
    }
    btn.disabled = false;
    btn.textContent = '📥 Pobierz ZIP';
}

async function bulkSendKindle() {
    if (selectedBooks.size === 0) return;
    const btn = document.getElementById('bulk-kindle');
    btn.disabled = true;
    btn.textContent = 'Wysyłam...';
    const targetFmt = document.getElementById('kindle-target-fmt')?.value || 'epub';
    const items = Array.from(selectedBooks.entries()).map(([id, {book, format}]) => ({
        id, title: book.title, author: book.author, format: format.toLowerCase(),
        target_format: targetFmt
    }));
    try {
        const resp = await fetch('/api/library/kindle', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({items})
        });
        if (resp.status === 401) { location.href = '/login'; return; }
        const data = await resp.json();
        if (data.success) {
            showToast('Dodano ' + data.added + ' do kolejki Kindle (' + targetFmt.toUpperCase() + ')');
        } else {
            showToast(data.error || 'Błąd', true);
        }
    } catch(e) {
        showToast(e.message, true);
    }
    btn.disabled = false;
    btn.textContent = '📱 Kindle';
}

function showToast(msg, isError) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast' + (isError ? ' error' : '');
    t.style.display = 'block';
    setTimeout(() => t.style.display = 'none', 4000);
}

function esc(s) {
    return s ? String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') : '';
}

async function loadLibrary() {
    try {
        const resp = await fetch('/api/library');
        if (resp.status === 401) { location.href = '/login'; return; }
        const data = await resp.json();
        if (data.error && !data.books.length) {
            document.getElementById('lib-content').innerHTML =
                '<div class="status">⚠️ ' + esc(data.error) + '</div>';
            document.getElementById('lib-stats').textContent = '';
            return;
        }
        allBooks = data.books || [];
        filteredBooks = [...allBooks];
        sortBooks();
        document.getElementById('lib-content').innerHTML = renderTable(filteredBooks);
        updateStats();
    } catch(e) {
        document.getElementById('lib-content').innerHTML =
            '<div class="status">Błąd ładowania biblioteki: ' + esc(e.message) + '</div>';
    }
}

loadLibrary();
</script>
</body></html>
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
        _log_activity(
            "auth_error", "", "",
            f"Failed login attempt for user '{username}' from {request.remote_addr}"
        )
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
    calibre_error, calibre_success = "", ""
    kindle = _get_user_kindle_settings(user)
    calibre_settings = _load_calibre_settings()

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
            _save_user_kindle_settings(user, kindle)
            kindle_success = "Ustawienia Kindle zapisane!"

        elif form_type == "calibre":
            calibre_settings = {
                "library_path": request.form.get("calibre_library_path", CALIBRE_LIBRARY_PATH).strip(),
            }
            _save_calibre_settings(calibre_settings)
            calibre_success = "Ustawienia Calibre zapisane!"

    return render_template_string(
        SETTINGS_TEMPLATE, user=user,
        pw_error=pw_error, pw_success=pw_success,
        kindle_error=kindle_error, kindle_success=kindle_success,
        calibre_error=calibre_error, calibre_success=calibre_success,
        kindle=type("K", (), kindle)(),
        calibre_settings=type("C", (), calibre_settings)(),
    )

@app.route("/kindle-queue")
@login_required
def kindle_queue_page():
    user = _get_current_user()
    queue = _load_kindle_queue()
    user_queue = [item for item in queue if item.get("user") == user]
    order = {"pending": 0, "found": 0, "sending": 0, "failed": 1, "sent": 2}
    user_queue.sort(key=lambda x: order.get(x.get("status", "pending"), 3))

    def truncate_list(lst, n):
        return lst[:n]

    app.jinja_env.filters["truncate_list"] = truncate_list

    return render_template_string(KINDLE_QUEUE_TEMPLATE, user=user, queue=user_queue)

@app.route("/logs")
@login_required
def logs_page():
    user = _get_current_user()
    return render_template_string(LOGS_TEMPLATE, user=user)

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
    max_pages = min(int(request.args.get("pages", 3)), 5)
    if not q: return jsonify([])
    try:
        results = search_annas(q, lang=lang, ext=ext, max_pages=max_pages)
        for r in results:
            r["calibre_status"] = check_calibre_status(r["title"], r.get("author", ""))
        return jsonify(results)
    except Exception as e:
        app.logger.error(f"Search error: {e}")
        _log_activity(
            "search_error", "", "",
            f"Search exception for query='{q}': {type(e).__name__}: {e}",
            user=_get_current_user()
        )
        return jsonify({"error": str(e)}), 500

@app.route("/api/download", methods=["POST"])
@login_required
def api_download():
    user = _get_current_user()
    data = request.get_json() or {}
    md5 = data.get("md5", "")
    if not md5:
        return jsonify({"error": "No MD5", "success": False})
    send_to_kindle = data.get("send_to_kindle", True)
    title = data.get("title", "")
    author = data.get("author", "")
    fmt = data.get("format", "epub")
    target_format = data.get("target_format", "epub")
    result = download_via_stacks(md5)
    if result.get("error") and not result.get("success", True):
        _log_activity(
            "download_error", title, author,
            f"Stacks download failed: {result['error']}",
            user=user, md5=md5
        )
    if send_to_kindle and title:
        _add_to_kindle_queue(md5, title, author, fmt, user, target_format=target_format)
        _log_activity("kindle_queue", title, author,
                      f"Dodano do kolejki Kindle (format docelowy: {target_format.upper()})",
                      user=user, md5=md5)
    else:
        _log_activity("download", title, author,
                      f"Wysłano do Stacks/Calibre ({fmt.upper()})",
                      user=user, md5=md5)
    return jsonify(result)


@app.route("/api/download/bulk", methods=["POST"])
@login_required
def api_download_bulk():
    user = _get_current_user()
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
        author = item.get("author", "")
        fmt = item.get("format", "epub")
        target_format = item.get("target_format", fmt)
        result = download_via_stacks(md5)
        if result.get("error") and not result.get("success", True):
            _log_activity(
                "download_error", title, author,
                f"Stacks download failed (bulk): {result['error']}",
                user=user, md5=md5
            )
        if send_to_kindle and title:
            _add_to_kindle_queue(md5, title, author, fmt, user, target_format=target_format)
            _log_activity("kindle_queue", title, author,
                          f"Dodano do kolejki Kindle (format: {target_format.upper()})",
                          user=user, md5=md5)
        else:
            _log_activity("download", title, author,
                          f"Wysłano do Stacks/Calibre ({fmt.upper()})",
                          user=user, md5=md5)
        results.append(result)
    return jsonify({"results": results})


@app.route("/api/kindle/queue")
@login_required
def api_kindle_queue():
    user = _get_current_user()
    queue = _load_kindle_queue()
    user_queue = [item for item in queue if item.get("user") == user]
    return jsonify(user_queue)


@app.route("/api/kindle/queue/<md5>", methods=["DELETE"])
@login_required
def api_kindle_queue_delete(md5):
    user = _get_current_user()
    queue = _load_kindle_queue()
    original_len = len(queue)
    queue = [item for item in queue if not (item["md5"] == md5 and item.get("user") == user)]
    if len(queue) < original_len:
        with _queue_lock:
            _save_kindle_queue(queue)
        return jsonify({"success": True})
    return jsonify({"error": "Not found", "success": False}), 404


@app.route("/api/kindle/queue/<md5>/retry", methods=["POST"])
@login_required
def api_kindle_queue_retry(md5):
    user = _get_current_user()
    queue = _load_kindle_queue()
    for item in queue:
        if item["md5"] == md5 and item.get("user") == user:
            item["status"] = "pending"
            item["error"] = None
            item["attempts"] = 0
            with _queue_lock:
                _save_kindle_queue(queue)
            return jsonify({"success": True})
    return jsonify({"error": "Not found", "success": False}), 404


@app.route("/library")
@login_required
def library_page():
    user = _get_current_user()
    return render_template_string(LIBRARY_TEMPLATE, user=user)


@app.route("/api/library")
@login_required
def api_library():
    """Return all books from Calibre library with metadata."""
    db_path = _get_calibre_db_path()
    if not os.path.exists(db_path):
        return jsonify({"error": "Calibre library not found", "books": []})
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.execute("""
            SELECT b.id, b.title, b.path, b.timestamp,
                   GROUP_CONCAT(DISTINCT a.name) as authors,
                   GROUP_CONCAT(DISTINCT d.format || ':' || d.name || ':' || d.uncompressed_size) as formats
            FROM books b
            LEFT JOIN books_authors_link bal ON b.id = bal.book
            LEFT JOIN authors a ON bal.author = a.id
            LEFT JOIN data d ON b.id = d.book
            GROUP BY b.id
            ORDER BY b.timestamp DESC
        """)
        books = []
        for row in cursor.fetchall():
            book_id, title, path, timestamp, authors, formats_str = row
            if not formats_str:
                continue
            formats = []
            for fmt_info in formats_str.split(','):
                parts = fmt_info.split(':')
                if len(parts) >= 3:
                    formats.append({
                        "format": parts[0],
                        "name": parts[1],
                        "size": int(parts[2]) if parts[2].isdigit() else 0
                    })
            if not formats:
                continue
            books.append({
                "id": book_id,
                "title": title,
                "author": authors or "Unknown",
                "path": path,
                "added": timestamp[:10] if timestamp else "",
                "formats": formats
            })
        conn.close()
        return jsonify({"books": books})
    except Exception as e:
        app.logger.error(f"Library error: {e}")
        _log_activity(
            "error", "", "",
            f"Library API error: {type(e).__name__}: {e}",
            user=_get_current_user()
        )
        return jsonify({"error": str(e), "books": []})


@app.route("/api/library/download", methods=["POST"])
@login_required
def api_library_download():
    """Download selected books as ZIP."""
    user = _get_current_user()
    data = request.get_json() or {}
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "No items selected"}), 400

    library_path = _load_calibre_settings().get("library_path", CALIBRE_LIBRARY_PATH)
    db_path = _get_calibre_db_path()

    if not os.path.exists(db_path):
        return jsonify({"error": "Calibre library not found"}), 404

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

        zip_buffer = io.BytesIO()
        used_names = set()

        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for item in items:
                book_id = item.get("id")
                fmt = item.get("format", "EPUB").upper()

                cursor = conn.execute(
                    "SELECT b.title, b.path, d.name, d.format "
                    "FROM books b JOIN data d ON b.id = d.book "
                    "WHERE b.id = ? AND d.format = ? COLLATE NOCASE",
                    (book_id, fmt)
                )
                row = cursor.fetchone()
                if not row:
                    continue

                title, book_path, file_name, file_format = row

                author_cursor = conn.execute(
                    "SELECT a.name FROM authors a "
                    "JOIN books_authors_link bal ON a.id = bal.author "
                    "WHERE bal.book = ? LIMIT 1", (book_id,)
                )
                author_row = author_cursor.fetchone()
                author = author_row[0] if author_row else "Unknown"

                filepath = os.path.join(library_path, book_path, f"{file_name}.{file_format.lower()}")
                if not os.path.exists(filepath):
                    app.logger.warning(f"File not found: {filepath}")
                    continue

                clean_title = re.sub(r'[<>:"/\\|?*]', '', title).strip()
                clean_author = re.sub(r'[<>:"/\\|?*]', '', author).strip()
                zip_name = f"{clean_author} - {clean_title}.{file_format.lower()}"

                base_name = zip_name
                counter = 2
                while zip_name in used_names:
                    name_part = base_name.rsplit('.', 1)
                    zip_name = f"{name_part[0]} ({counter}).{name_part[1]}"
                    counter += 1
                used_names.add(zip_name)

                zf.write(filepath, zip_name)

        conn.close()
        zip_buffer.seek(0)

        return send_file(
            zip_buffer,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'booksearch-export-{date.today().isoformat()}.zip'
        )
    except Exception as e:
        app.logger.error(f"Library download error: {e}")
        _log_activity(
            "error", "", "",
            f"Library ZIP download error: {type(e).__name__}: {e}",
            user=user
        )
        return jsonify({"error": str(e)}), 500


@app.route("/api/library/kindle", methods=["POST"])
@login_required
def api_library_kindle():
    """Add selected library books to Kindle queue."""
    data = request.get_json() or {}
    items = data.get("items", [])
    user = _get_current_user()

    try:
        added = 0
        for item in items:
            target_format = item.get("target_format", item.get("format", "epub"))
            _add_to_kindle_queue(
                md5=f"calibre-{item['id']}",
                title=item.get("title", ""),
                author=item.get("author", ""),
                fmt=item.get("format", "epub"),
                user=user,
                target_format=target_format
            )
            _log_activity(
                "kindle_queue",
                item.get("title", ""),
                item.get("author", ""),
                f"Dodano z biblioteki do kolejki Kindle (format: {target_format.upper()})",
                user=user
            )
            added += 1

        return jsonify({"success": True, "added": added})
    except Exception as e:
        app.logger.error(f"Library Kindle queue error: {e}")
        _log_activity(
            "error", "", "",
            f"Library Kindle queue error: {type(e).__name__}: {e}",
            user=user
        )
        return jsonify({"error": str(e), "success": False}), 500


@app.route("/api/logs")
@login_required
def api_logs():
    """Return activity log entries. Optional: ?type=...&limit=100&offset=0"""
    type_filter = request.args.get("type", "")
    try:
        limit = int(request.args.get("limit", 100))
    except ValueError:
        limit = 100
    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0

    with _queue_lock:
        log = _load_activity_log()

    if type_filter:
        log = [e for e in log if e.get("type") == type_filter]

    total = len(log)
    log_slice = log[offset:offset + limit]

    return jsonify({"logs": log_slice, "total": total, "limit": limit, "offset": offset})


@app.route("/api/logs", methods=["DELETE"])
@login_required
def api_logs_clear():
    """Clear all activity logs."""
    with _queue_lock:
        _save_activity_log([])
    return jsonify({"success": True})


@app.route("/api/stacks/status")
@login_required
def api_stacks_status():
    """Proxy Stacks /api/status endpoint."""
    try:
        req = urllib.request.Request(
            f"{STACKS_URL}/api/status",
            headers={"Accept": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=8)
        data = json.loads(resp.read())
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e), "queue": [], "current_downloads": [], "recent_history": []}), 503


# -- Error handlers ------------------------------------------------------------

@app.errorhandler(500)
def handle_500(e):
    try:
        user = _get_current_user()
    except Exception:
        user = None
    _log_activity("server_error", "", "", f"Internal server error: {type(e).__name__}: {e}", user=user)
    return jsonify({"error": "Internal server error"}), 500

@app.errorhandler(Exception)
def handle_exception(e):
    # Pass through HTTP errors (404, 401, etc.) — only catch unexpected ones
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    try:
        user = _get_current_user()
    except Exception:
        user = None
    _log_activity("server_error", "", "", f"Unhandled exception: {type(e).__name__}: {e}", user=user)
    app.logger.error(f"Unhandled exception: {e}", exc_info=True)
    return jsonify({"error": "Internal server error"}), 500


# -- Startup -------------------------------------------------------------------

# Migrate global kindle settings to per-user on first boot
_migrate_global_kindle_settings()

# Start background Kindle polling thread (handles both Kindle queue and Stacks polling)
poll_thread = threading.Thread(target=kindle_poll_worker, daemon=True)
poll_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
