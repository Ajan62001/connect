#!/bin/sh
# connect backup script (runtime design §6). Invoked by crond:
#
#   backup.sh pg     02:30 UTC  pg_dump -Fc -> /backups/pg/connect-YYYYMMDD.dump
#   backup.sh blobs  03:00 UTC  rsync -a --link-dest=<yesterday's snapshot>
#                               /blobs -> /backups/blobs/YYYYMMDD/
#
# Blobs are content-addressed and never mutate, so --link-dest hardlink
# snapshots cost almost nothing: unchanged files share inodes with the
# previous snapshot; only new blobs occupy space.
#
# Retention (both targets, pruned on every run): keep the last 7 daily
# snapshots, plus Sunday snapshots up to 28 days old (= 4 weeklies).
# Connection/auth comes from the standard PG* env vars set in compose.
#
# /backups is its own volume; on a VPS point it at a separate disk and/or
# rsync it offsite — documented in the README, not automated here.
set -eu

TODAY="$(date -u +%Y%m%d)"

# keep_date DATE -> 0 (keep) / 1 (prune)
keep_date() {
    d="$1"
    cutoff_daily="$(date -u -d '7 days ago' +%Y%m%d)"
    cutoff_weekly="$(date -u -d '28 days ago' +%Y%m%d)"
    [ "$d" -ge "$cutoff_daily" ] && return 0
    if [ "$d" -ge "$cutoff_weekly" ]; then
        dow="$(date -u -d "$d" +%u)" || return 1
        [ "$dow" = "7" ] && return 0
    fi
    return 1
}

prune() {
    # $1 = directory of entries whose names contain a YYYYMMDD stamp
    dir="$1"
    [ -d "$dir" ] || return 0
    for path in "$dir"/*; do
        [ -e "$path" ] || continue
        stamp="$(basename "$path" | grep -oE '[0-9]{8}' | head -1 || true)"
        [ -n "$stamp" ] || continue
        if ! keep_date "$stamp"; then
            echo "prune: $path"
            rm -rf "$path"
        fi
    done
}

case "${1:?usage: backup.sh pg|blobs}" in
pg)
    mkdir -p /backups/pg
    tmp="/backups/pg/.connect-$TODAY.dump.part"
    pg_dump -Fc -f "$tmp"
    mv "$tmp" "/backups/pg/connect-$TODAY.dump"
    echo "pg backup done: connect-$TODAY.dump ($(du -h "/backups/pg/connect-$TODAY.dump" | cut -f1))"
    prune /backups/pg
    ;;
blobs)
    mkdir -p /backups/blobs
    dest="/backups/blobs/$TODAY"
    tmp="/backups/blobs/.${TODAY}.part"
    rm -rf "$tmp"
    prev="$(find /backups/blobs -mindepth 1 -maxdepth 1 -type d ! -name '.*' | sort | tail -1 || true)"
    if [ -n "$prev" ]; then
        rsync -a --delete --link-dest="$prev" /blobs/ "$tmp/"
    else
        rsync -a /blobs/ "$tmp/"
    fi
    rm -rf "$dest"
    mv "$tmp" "$dest"
    echo "blob snapshot done: $dest ($(du -sh "$dest" | cut -f1) apparent)"
    prune /backups/blobs
    ;;
*)
    echo "usage: backup.sh pg|blobs" >&2
    exit 2
    ;;
esac
