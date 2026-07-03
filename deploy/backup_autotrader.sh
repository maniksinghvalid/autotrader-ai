#!/bin/zsh
# AutoTrader nightly backup — SQLite projection + trade audit journal(s).
#
# Run by launchd (com.autotrader.backup.plist, 17:30 local) or manually for a
# restore drill. Idempotent per day (dated filenames just get overwritten on
# re-run). Backs up:
#   - the SQLite projection, via `VACUUM INTO` (a live-safe, consistent
#     on-disk copy — no need to stop the trader process first)
#   - the current trade-audit journal (AUTOTRADER_DB_PATH's sibling audit
#     file, `~/.autotrader_trade_audit.jsonl` — see Task 20 for the rename
#     that makes this the live default) and, if still present, the legacy
#     `~/.futu_trade_audit.jsonl` (today's actual live audit path, and the
#     one still shared with the vendored Futu skills)
# then prunes backups older than 30 days.
set -euo pipefail

DB_PATH="${AUTOTRADER_DB_PATH:-$HOME/.autotrader.db}"
AUDIT_PATH="${AUTOTRADER_AUDIT_PATH:-$HOME/.autotrader_trade_audit.jsonl}"
LEGACY_AUDIT_PATH="$HOME/.futu_trade_audit.jsonl"
BACKUP_DIR="${AUTOTRADER_BACKUP_DIR:-$HOME/.autotrader_backups}"
RETENTION_DAYS=30
STAMP="$(date +%F)"

mkdir -p "$BACKUP_DIR"

echo "[$(date -u +%FT%TZ)] backup starting -> $BACKUP_DIR"

if [ -f "$DB_PATH" ]; then
    sqlite3 "$DB_PATH" "VACUUM INTO '$BACKUP_DIR/autotrader-$STAMP.db'"
    echo "  DB backed up: autotrader-$STAMP.db"
else
    echo "  WARNING: DB not found at $DB_PATH — skipped" >&2
fi

if [ -f "$AUDIT_PATH" ]; then
    cp "$AUDIT_PATH" "$BACKUP_DIR/autotrader_trade_audit-$STAMP.jsonl"
    echo "  audit journal backed up: autotrader_trade_audit-$STAMP.jsonl"
else
    echo "  audit journal not found at $AUDIT_PATH — skipped (not yet renamed?)"
fi

if [ -f "$LEGACY_AUDIT_PATH" ]; then
    cp "$LEGACY_AUDIT_PATH" "$BACKUP_DIR/futu_trade_audit-$STAMP.jsonl"
    echo "  legacy audit journal backed up: futu_trade_audit-$STAMP.jsonl"
fi

# Prune backups older than RETENTION_DAYS. -mtime +N in find(1) means "more
# than N*24h old"; guard the whole prune behind the directory actually
# existing (mkdir -p above already guarantees this, but keep it defensive).
if [ -d "$BACKUP_DIR" ]; then
    find "$BACKUP_DIR" -type f -mtime "+$RETENTION_DAYS" -print -delete | while read -r f; do
        echo "  pruned old backup: $f"
    done
fi

echo "[$(date -u +%FT%TZ)] backup complete"
