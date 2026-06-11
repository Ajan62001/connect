# Backup sidecar (runtime design §6) — prod profile only.
# alpine + postgresql17-client (matches the pgvector:pg17 server) + rsync,
# driven by busybox crond. coreutils for GNU date (retention math).
FROM alpine:3.22

RUN apk add --no-cache postgresql17-client rsync coreutils

COPY backup.sh /usr/local/bin/backup.sh
COPY crontab /etc/crontabs/root
RUN chmod +x /usr/local/bin/backup.sh

# -f foreground, -l 2: log to stdout at notice level
CMD ["crond", "-f", "-l", "2"]
