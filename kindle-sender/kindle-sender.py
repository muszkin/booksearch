#!/usr/bin/env python3
"""
Kindle Auto-Sender
==================
Watches Calibre library for new books.
Sends EPUB files to Kindle via email.
Converts other formats (PDF, MOBI, AZW3, etc.) to EPUB using ebook-convert.
Reads SMTP config from /data/kindle-settings.json (shared with BookSearch UI).
"""
import os, time, smtplib, logging, json, subprocess
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

CALIBRE_LIBRARY = Path(os.environ.get("CALIBRE_LIBRARY", "/library"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SETTINGS_FILE = DATA_DIR / "kindle-settings.json"
STATE_FILE = DATA_DIR / ".kindle-sent.json"
CONVERT_DIR = DATA_DIR / ".convert-tmp"

NO_KINDLE_FILE = DATA_DIR / "no-kindle.txt"
CONVERTIBLE = {".pdf", ".mobi", ".azw", ".azw3", ".doc", ".docx", ".fb2", ".rtf", ".txt", ".htmlz"}
MIN_SIZE = 5 * 1024
CONVERT_TIMEOUT = 300

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)


def load_no_kindle_titles():
    if not NO_KINDLE_FILE.exists():
        return []
    try:
        lines = NO_KINDLE_FILE.read_text(encoding="utf-8").strip().splitlines()
        return [line.strip().lower() for line in lines if line.strip()]
    except OSError:
        return []


def is_no_kindle(filename):
    titles = load_no_kindle_titles()
    if not titles:
        return False
    filename_lower = filename.lower()
    return any(title in filename_lower for title in titles)


def load_config():
    if not SETTINGS_FILE.exists():
        return None
    try:
        cfg = json.loads(SETTINGS_FILE.read_text())
        if not cfg.get("enabled"):
            return None
        required = ["kindle_email", "smtp_host", "smtp_port", "smtp_email", "smtp_password"]
        if not all(cfg.get(k) for k in required):
            return None
        return cfg
    except (json.JSONDecodeError, OSError):
        return None


def load_sent():
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def save_sent(sent):
    STATE_FILE.write_text(json.dumps(list(sent)))


def convert_to_epub(filepath):
    CONVERT_DIR.mkdir(parents=True, exist_ok=True)
    epub_name = filepath.stem + ".epub"
    epub_path = CONVERT_DIR / epub_name

    log.info(f"Converting {filepath.suffix} -> EPUB: {filepath.name}")

    try:
        result = subprocess.run(
            ["ebook-convert", str(filepath), str(epub_path)],
            capture_output=True, text=True, timeout=CONVERT_TIMEOUT,
        )

        if result.returncode != 0:
            log.error(f"Conversion failed: {filepath.name}\n{(result.stderr or result.stdout)[-500:]}")
            return None

        if not epub_path.exists() or epub_path.stat().st_size < MIN_SIZE:
            log.error(f"Conversion produced too small file: {filepath.name}")
            return None

        log.info(f"Converted: {filepath.name} -> {epub_name} ({epub_path.stat().st_size // 1024} KB)")
        return epub_path

    except subprocess.TimeoutExpired:
        log.error(f"Conversion timeout ({CONVERT_TIMEOUT}s): {filepath.name}")
        return None
    except Exception as e:
        log.error(f"Conversion error {filepath.name}: {e}")
        return None


def send_to_kindle(filepath, cfg):
    filepath = Path(filepath)
    if not filepath.exists():
        return False
    if filepath.stat().st_size < MIN_SIZE:
        log.warning(f"File too small, skipping: {filepath.name} ({filepath.stat().st_size}B)")
        return False

    log.info(f"Sending to Kindle: {filepath.name} ({filepath.stat().st_size // 1024} KB)")

    try:
        msg = MIMEMultipart()
        msg["From"] = cfg["smtp_email"]
        msg["To"] = cfg["kindle_email"]
        msg["Subject"] = ""

        part = MIMEBase("application", "epub+zip")
        part.set_payload(filepath.read_bytes())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{filepath.name}"')
        msg.attach(part)

        with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"])) as server:
            server.starttls()
            server.login(cfg["smtp_email"], cfg["smtp_password"])
            server.sendmail(cfg["smtp_email"], cfg["kindle_email"], msg.as_string())

        log.info(f"Sent: {filepath.name}")
        return True

    except Exception as e:
        log.error(f"Send error {filepath.name}: {e}")
        return False


def process_file(filepath, sent, cfg):
    suffix = filepath.suffix.lower()

    if suffix == ".epub":
        return send_to_kindle(filepath, cfg)

    if suffix in CONVERTIBLE:
        epub_path = convert_to_epub(filepath)
        if epub_path:
            success = send_to_kindle(epub_path, cfg)
            try:
                epub_path.unlink()
            except OSError:
                pass
            return success
        return False

    return False


class BookHandler(FileSystemEventHandler):
    def __init__(self):
        self.sent = load_sent()

    def _handle(self, filepath):
        suffix = filepath.suffix.lower()
        if suffix != ".epub" and suffix not in CONVERTIBLE:
            return

        if filepath.name.startswith(".") or filepath.name.startswith("~"):
            return

        if CONVERT_DIR in filepath.parents:
            return

        time.sleep(3)

        if not filepath.exists():
            return

        if filepath.stat().st_size < MIN_SIZE:
            return

        key = str(filepath)
        if key in self.sent:
            return

        cfg = load_config()
        if not cfg:
            log.debug("Kindle sender disabled or not configured, skipping")
            return

        if is_no_kindle(filepath.name):
            log.info(f"Skipping (no-kindle list): {filepath.name}")
            return

        if process_file(filepath, self.sent, cfg):
            self.sent.add(key)
            save_sent(self.sent)

    def on_created(self, event):
        if event.is_directory:
            return
        self._handle(Path(event.src_path))

    def on_moved(self, event):
        if event.is_directory:
            return
        self._handle(Path(event.dest_path))


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONVERT_DIR.mkdir(parents=True, exist_ok=True)
    CALIBRE_LIBRARY.mkdir(parents=True, exist_ok=True)

    log.info("=" * 50)
    log.info("Kindle Auto-Sender started")
    log.info(f"Watching:  {CALIBRE_LIBRARY}")
    log.info(f"Config:    {SETTINGS_FILE}")
    log.info(f"Formats:   EPUB (direct) + {', '.join(sorted(CONVERTIBLE))} (auto-convert)")
    log.info("=" * 50)

    cfg = load_config()
    if cfg:
        log.info(f"Kindle target: {cfg['kindle_email']}")
    else:
        log.info("Kindle sender not configured yet — waiting for config via BookSearch UI")

    handler = BookHandler()
    observer = Observer()
    observer.schedule(handler, str(CALIBRE_LIBRARY), recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
