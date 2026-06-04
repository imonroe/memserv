#!/bin/sh
set -eu

# crond runs jobs with a minimal environment, so snapshot the container's
# relevant env vars to a file that each job sources before running.
printenv | grep -E '^(MEM0_|DIGEST_|ANTHROPIC_)' > /etc/digest.env || true

SCHEDULE="${DIGEST_CRON:-0 8 * * *}"
echo "$SCHEDULE set -a; . /etc/digest.env; set +a; python /app/digest.py >> /var/log/digest.log 2>&1" \
    > /etc/crontabs/root

touch /var/log/digest.log
echo "digest: scheduled '$SCHEDULE' (UTC)." >> /var/log/digest.log

# Optionally run once at startup (handy to verify config right after deploy).
if [ "${DIGEST_RUN_ON_START:-false}" = "true" ]; then
    ( set -a; . /etc/digest.env; set +a; python /app/digest.py ) >> /var/log/digest.log 2>&1 || true
fi

# Surface the digest log through `docker logs` / `caprover logs`.
tail -F /var/log/digest.log &
exec crond -f -l 8
