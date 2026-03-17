#!/usr/bin/env python3
"""
Kindle Auto-Sender
==================
Obserwuje folder Calibre library na nowe książki.
- Wysyła TYLKO pliki EPUB
- Inne formaty (PDF, MOBI, AZW3, DOC) konwertuje na EPUB przed wysyłką
- Konwersja przez ebook-convert (Calibre CLI)
"""
import os, time, smtplib, logging, json, subprocess, shutil
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ── Konfiguracja ──────────────────────────────────────────────────────────────
CALIBRE_LIBRARY = Path("/mnt/nas/books/library")
STATE_FILE      = Path("/mnt/nas/books/.kindle-sent.json")
LOG_FILE        = Path("/mnt/nas/books/kindle-sender.log")
CONVERT_DIR     = Path("/mnt/nas/books/.convert-tmp")

KINDLE_EMAIL  = "muszkin@kindle.com"
SMTP_FROM     = "muszkin@gmail.com"
SMTP_PASSWORD = "iepoxpobxxvcuvel"
SMTP_HOST     = "smtp.gmail.com"
SMTP_PORT     = 587

# Formaty do konwersji na EPUB
CONVERTIBLE = {".pdf", ".mobi", ".azw", ".azw3", ".doc", ".docx", ".fb2", ".rtf", ".txt", ".htmlz"}

# Minimum rozmiar — pomijaj pliki poniżej 5KB (okładki, metadane)
MIN_SIZE = 5 * 1024

# Timeout konwersji (sekundy)
CONVERT_TIMEOUT = 300

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

def load_sent():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()

def save_sent(sent):
    STATE_FILE.write_text(json.dumps(list(sent)))

def convert_to_epub(filepath: Path) -> Path | None:
    """Konwertuje plik na EPUB przez ebook-convert. Zwraca ścieżkę do EPUB lub None."""
    CONVERT_DIR.mkdir(parents=True, exist_ok=True)
    epub_name = filepath.stem + ".epub"
    epub_path = CONVERT_DIR / epub_name

    log.info(f"🔄 Konwertuję {filepath.suffix} → EPUB: {filepath.name}")

    try:
        # ebook-convert jest w kontenerze Docker "calibre"
        # /books w kontenerze = /mnt/nas/books/library na hoście
        # /tmp/convert w kontenerze montujemy jako bind
        container_input = str(filepath).replace(str(CALIBRE_LIBRARY), "/books")
        container_output = f"/tmp/{epub_name}"

        result = subprocess.run(
            ["docker", "exec", "calibre", "ebook-convert", container_input, container_output],
            capture_output=True, text=True, timeout=CONVERT_TIMEOUT
        )

        # Kopiuj wynik z kontenera
        if result.returncode == 0:
            subprocess.run(
                ["docker", "cp", f"calibre:{container_output}", str(epub_path)],
                capture_output=True, timeout=30
            )
            # Posprzątaj w kontenerze
            subprocess.run(
                ["docker", "exec", "calibre", "rm", "-f", container_output],
                capture_output=True, timeout=10
            )
        if result.returncode != 0:
            log.error(f"❌ Konwersja nieudana: {filepath.name}\n{result.stderr[-500:] if result.stderr else result.stdout[-500:]}")
            return None

        if not epub_path.exists() or epub_path.stat().st_size < MIN_SIZE:
            log.error(f"❌ Konwersja dała za mały plik: {filepath.name}")
            return None

        log.info(f"✅ Skonwertowano: {filepath.name} → {epub_name} ({epub_path.stat().st_size // 1024} KB)")
        return epub_path

    except subprocess.TimeoutExpired:
        log.error(f"❌ Timeout konwersji ({CONVERT_TIMEOUT}s): {filepath.name}")
        return None
    except Exception as e:
        log.error(f"❌ Błąd konwersji {filepath.name}: {e}")
        return None

def send_to_kindle(filepath: Path) -> bool:
    """Wysyła plik EPUB na Kindle przez email."""
    filepath = Path(filepath)
    if not filepath.exists():
        return False
    if filepath.stat().st_size < MIN_SIZE:
        log.warning(f"Plik za mały, pomijam: {filepath.name} ({filepath.stat().st_size}B)")
        return False

    log.info(f"📧 Wysyłam na Kindle: {filepath.name} ({filepath.stat().st_size // 1024} KB)")

    try:
        msg = MIMEMultipart()
        msg["From"]    = SMTP_FROM
        msg["To"]      = KINDLE_EMAIL
        msg["Subject"] = ""

        part = MIMEBase("application", "epub+zip")
        part.set_payload(filepath.read_bytes())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{filepath.name}"')
        msg.attach(part)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_FROM, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, KINDLE_EMAIL, msg.as_string())

        log.info(f"✅ Wysłano: {filepath.name}")
        return True

    except Exception as e:
        log.error(f"❌ Błąd wysyłki {filepath.name}: {e}")
        return False

def process_file(filepath: Path, sent: set) -> bool:
    """Przetwarza plik — konwertuje jeśli trzeba, wysyła EPUB."""
    suffix = filepath.suffix.lower()

    # Już EPUB — wysyłaj bezpośrednio
    if suffix == ".epub":
        return send_to_kindle(filepath)

    # Inny format — konwertuj na EPUB
    if suffix in CONVERTIBLE:
        epub_path = convert_to_epub(filepath)
        if epub_path:
            success = send_to_kindle(epub_path)
            # Posprzątaj tymczasowy plik
            try:
                epub_path.unlink()
            except:
                pass
            return success
        return False

    # Nieobsługiwany format
    log.debug(f"Pomijam nieobsługiwany format: {filepath.name}")
    return False

class BookHandler(FileSystemEventHandler):
    def __init__(self):
        self.sent = load_sent()

    def _handle(self, filepath: Path):
        suffix = filepath.suffix.lower()
        if suffix != ".epub" and suffix not in CONVERTIBLE:
            return

        # Pomijaj pliki tymczasowe
        if filepath.name.startswith(".") or filepath.name.startswith("~"):
            return

        # Pomijaj folder konwersji
        if CONVERT_DIR in filepath.parents:
            return

        # Poczekaj aż plik będzie w pełni zapisany
        time.sleep(3)

        if not filepath.exists():
            return

        if filepath.stat().st_size < MIN_SIZE:
            log.warning(f"Plik za mały, pomijam: {filepath.name} ({filepath.stat().st_size}B)")
            return

        key = str(filepath)
        if key in self.sent:
            return

        if process_file(filepath, self.sent):
            self.sent.add(key)
            save_sent(self.sent)

    def on_created(self, event):
        if event.is_directory:
            return
        self._handle(Path(event.src_path))

    def on_moved(self, event):
        """Calibre często najpierw tworzy plik tymczasowy, potem go przenosi."""
        if event.is_directory:
            return
        self._handle(Path(event.dest_path))

if __name__ == "__main__":
    CONVERT_DIR.mkdir(parents=True, exist_ok=True)

    log.info("=" * 50)
    log.info("Kindle Auto-Sender uruchomiony")
    log.info(f"Obserwuję:  {CALIBRE_LIBRARY}")
    log.info(f"Kindle:     {KINDLE_EMAIL}")
    log.info(f"Wysyłam:    tylko EPUB (inne formaty → auto-konwersja)")
    log.info(f"Konwersja:  ebook-convert (Calibre CLI)")
    log.info("=" * 50)

    CALIBRE_LIBRARY.mkdir(parents=True, exist_ok=True)

    handler  = BookHandler()
    observer = Observer()
    observer.schedule(handler, str(CALIBRE_LIBRARY), recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
