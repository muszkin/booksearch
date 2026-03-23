# Changelog

## v0.7 — 2026-03-23

### Added
- **Stacks authentication** — BookSearch now authenticates with Stacks API using session cookies (login + auto re-login on 401)
- **Comprehensive error logging** — auth failures, search errors, download errors, SMTP failures, conversion errors, and unhandled exceptions all logged with full details
- **New error log types** — 🔐 auth_error, 🔍 search_error, 📥 download_error, 💥 server_error with color-coded icons
- **Flask error handlers** — 500 and unhandled exceptions caught and logged
- **`STACKS_USER` / `STACKS_PASS` environment variables** — configurable Stacks credentials (default: admin/mucha2024)

### Fixed
- **Download 401 errors** — Stacks now requires authentication; BookSearch logs in automatically before queue/add requests
- **Kindle send error details** — SMTP errors returned as strings with full detail instead of generic "SMTP send failed"
- **Long error details truncated** — >500 char details trimmed (first 200 + last 200) to prevent log bloat

## v0.6.1 — 2026-03-23 (merged into v0.7)

### Fixed
- **Activity logs now capture errors** — auth failures, search errors, download errors, SMTP failures, conversion errors, and unhandled exceptions are all logged with full details
- **New error log types** — auth_error, search_error, download_error, server_error with color-coded icons in the /logs page
- **Flask error handlers** — 500 and unhandled exceptions are caught and logged to activity log
- **send_book_to_kindle() returns error details** — SMTP errors (auth failure, connection errors) are now returned as strings and logged with full detail instead of just "SMTP send failed"
- **Long error details truncated** — details strings >500 chars are truncated to first 200 + last 200 chars to prevent log bloat

## v0.6 — 2026-03-20

### Added
- **Format conversion for Kindle** — when sending to Kindle, choose target format (EPUB/MOBI/AZW3/PDF) via dropdown in search page and library page; `ebook-convert` (Calibre CLI) is used automatically when conversion is needed
- **Activity Logs** — new `/logs` page showing all events (downloads, Kindle sends, conversions, Stacks downloads) with color-coding and filtering
  - `GET /api/logs` — fetch log entries with optional `?type=...&limit=...&offset=...`
  - `DELETE /api/logs` — clear all logs
  - Auto-refresh toggle, "Load more" button, text search and type filter
- **Stacks queue integration** — `/logs` page shows live Stacks download status (progress bars, queue) refreshed every 5s; new `GET /api/stacks/status` proxy endpoint
- **Background Stacks polling** — worker thread polls Stacks `/api/status` every 30s and logs new completions automatically to activity log
- **"📋 Logi" link** — added to topbar in all templates (order: Szukaj | Biblioteka | 📱 Kolejka Kindle | 📋 Logi | Ustawienia | Wyloguj)
- **Kindle queue: `target_format` field** — queue items now store target format for conversion-on-send

### Changed
- `_add_to_kindle_queue()` now accepts `target_format` parameter (default "epub")
- `kindle_poll_worker` handles both Kindle queue and Stacks status polling in single thread
- Dockerfile: added `calibre` apt package for `ebook-convert` CLI support

### Technical
- `_log_activity()` helper — thread-safe activity logging with max 500 entries
- `/data/activity-log.json` — activity log storage
- `/data/stacks-seen.json` — tracks seen Stacks completions to avoid duplicate log entries
- Shared `_queue_lock` used for both queue and log operations

## v0.5 — 2026-03-20

### Added
- **Calibre Library Browser** — new page `/library` to browse the full Calibre library
- **Search & filter** — client-side filtering by title/author and format dropdown (EPUB/PDF/MOBI/AZW3/FB2)
- **Sortable columns** — click column headers to sort by title, author, size, or date added
- **Bulk ZIP download** — select multiple books and download them as a single ZIP file
  - Filename format: `Author - Title.format` (e.g. `Stanislaw Lem - Solaris.epub`)
  - ZIP filename: `booksearch-export-YYYY-MM-DD.zip`
  - Handles duplicate filenames by appending (2), (3) etc.
  - ZIP built in-memory (no temp files)
- **Bulk Kindle send from library** — send selected Calibre books directly to Kindle queue
- **Floating selection panel** — same sliding panel pattern as search page
- **Topbar updated** — "Biblioteka" link added to all pages (Szukaj, Biblioteka, Kolejka Kindle, Ustawienia)
- **Graceful error handling** — library page works even if metadata.db is empty or missing

## v0.4.1 — 2026-03-20

### Added
- **Floating selection panel** — when selecting books, a sliding panel appears on the right showing compact list (title + author) with individual remove buttons and bulk clear
- **Responsive** — panel adapts to mobile screens (narrower width)

### Fixed
- **Calibre library badges not working** — booksearch container was using an empty Docker volume instead of the actual Calibre library path; fixed volume mount in override so badges ("📖 Już w Calibre", "📗 Tytuł w bibliotece", "✍️ Autor w bibliotece") work correctly
- **docker-compose.override.yml** — removed leftover kindle-sender service reference

## v0.4 — 2026-03-20

### Added
- **Kindle sending integrated into BookSearch** — no more separate kindle-sender container
- **Per-user Kindle settings** — each user configures their own Kindle email and SMTP
- **Kindle send queue** — visual queue showing pending, sent, and failed items at `/kindle-queue`
- **Reliable book matching** — uses Calibre metadata.db instead of filename matching
- **Retry logic** — failed sends retry up to 3 times

### Removed
- `kindle-sender` container — no longer needed, sending is built into BookSearch
- `kindle-queue.txt` / `no-kindle.txt` file-based tracking — replaced by JSON queue

### Changed
- Kindle settings moved from global to per-user (auto-migrated from old format)
- Download API simplified — no more external file coordination

## v0.3.3 — 2026-03-20

### Fixed
- **Calibre-only books still sent to Kindle** — Inverted Kindle logic from blocklist to allowlist. Books are now only sent to Kindle when explicitly requested via the 📱 Kindle button. Previously, a `no-kindle.txt` blocklist was used (add when NOT sending), which was unreliable due to title matching issues between Anna's Archive results and Calibre's file structure. Now `kindle-queue.txt` acts as an allowlist (add only when sending to Kindle), and entries are removed after successful delivery to prevent re-triggering.

## v0.3.2 — 2026-03-20

### Fixed
- **Calibre-only books still sent to Kindle** — `normalize_text()` only stripped `_.-` characters, causing comma/punctuation mismatches between no-kindle list and filenames (e.g. "Gorzko, gorzko" vs "Gorzko gorzko"). Now strips ALL non-alphanumeric characters for robust matching.
- **No-kindle path matching** — `is_no_kindle()` now checks the full file path (including Calibre's `Author/Title/` directory structure), not just the filename
- **Normalization consistency** — both `booksearch` and `kindle-sender` now use identical `normalize_text()` logic

## v0.3.1 — 2026-03-20

### Fixed
- **Author search returning no results** — increased FlareSolverr timeout from 30s to 60s, added retry logic (2 attempts with 2s delay)
- **Empty responses silently swallowed** — added detailed logging for FlareSolverr failures (status, message, attempt number)

### Added
- **Pagination support** — search now fetches up to 3 pages from Anna's Archive (configurable via `pages` API param, max 5), collecting up to 50 results
- **Result count display** — UI now shows total number of results found
- **Better error logging** — distinguishes between URL errors, empty responses, and FlareSolverr status errors

### Changed
- Refactored HTML parsing into `_parse_results_from_html()` helper for cleaner pagination logic
- Search result limit increased from 25 to 50

## v0.3 — 2026-03-16

### Added
- Calibre library check — search results show badges when a book is already in Calibre library
- Calibre settings — configure library path in Settings page
- Calibre-only download — separate "📚 Calibre" and "📱 Kindle" buttons per result

### Fixed
- No-kindle diacritics matching (Polish characters ąćęłńóśźż)

## v0.2 — 2026-03-15

### Added
- Bulk selection with floating action bar
- Bulk download API (`POST /api/download/bulk`)
- Language flag emojis (🇵🇱 🇬🇧 🇩🇪 🇷🇺 🌍)
- No-Kindle list for Calibre-only downloads
- Distinct button colors (Calibre = purple, Kindle = green)
