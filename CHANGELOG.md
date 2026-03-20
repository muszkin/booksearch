# Changelog

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
