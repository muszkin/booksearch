#!/bin/bash
# Calibre Auto-Import: watches incoming folder, imports to Calibre library
INCOMING="${INCOMING_DIR:-/incoming}"
LIBRARY="${CALIBRE_LIBRARY:-/books}"
MIN_SIZE="${MIN_SIZE:-1024}"

echo "$(date '+%F %T') === Calibre auto-import started ==="
echo "$(date '+%F %T') Watching: $INCOMING"
echo "$(date '+%F %T') Library: $LIBRARY"

# Install inotify-tools if missing
which inotifywait > /dev/null 2>&1 || apk add --no-cache inotify-tools > /dev/null 2>&1

mkdir -p "$INCOMING" "$LIBRARY"

process_file() {
    local file="$1"
    local basename=$(basename "$file")
    local ext="${basename##*.}"
    local size=$(stat -c%s "$file" 2>/dev/null || echo 0)

    # Skip small files
    [ "$size" -lt "$MIN_SIZE" ] && return

    # Handle .bin files (Stacks sometimes saves as .bin)
    if [ "$ext" = "bin" ]; then
        local mime=$(file -b --mime-type "$file")
        case "$mime" in
            application/epub+zip) mv "$file" "${file%.bin}.epub"; file="${file%.bin}.epub"; ext="epub" ;;
            application/pdf) mv "$file" "${file%.bin}.pdf"; file="${file%.bin}.pdf"; ext="pdf" ;;
            *) echo "$(date '+%F %T') SKIP: $basename (unknown mime: $mime)"; return ;;
        esac
        basename=$(basename "$file")
        echo "$(date '+%F %T') RENAMED: $basename (was .bin)"
    fi

    # Only process ebook formats
    case "$ext" in
        epub|pdf|mobi|azw|azw3|doc|docx|fb2|rtf|cbz|cbr|txt) ;;
        *) return ;;
    esac

    echo "$(date '+%F %T') IMPORT: $basename (${size}B)"

    # Import to Calibre
    if calibredb add "$file" --library-path "$LIBRARY" 2>&1; then
        echo "$(date '+%F %T') OK: $basename -> Calibre"
        rm -f "$file"
        echo "$(date '+%F %T') DELETED: $basename from incoming"
    else
        echo "$(date '+%F %T') FAIL: $basename"
    fi
}

# Process existing files first
find "$INCOMING" -maxdepth 1 -type f | while read f; do
    process_file "$f"
done

# Watch for new files
inotifywait -m -r -e close_write,moved_to "$INCOMING" --format '%w%f' | while read file; do
    sleep 2  # Wait for file to be fully written
    [ -f "$file" ] && process_file "$file"
done
