#!/bin/sh
set -eu

# crond runs jobs with a minimal environment, so snapshot the container's
# relevant env vars to a file each job sources before running. Values can
# contain spaces (e.g. DIGEST_TITLE, DIGEST_CRON), so emit shell-safe,
# single-quote-escaped `export` lines rather than dumping raw printenv output.
# The file holds secrets (MEM0_API_KEY), so keep it private.
ENV_FILE=/etc/digest.env
: > "$ENV_FILE"
chmod 600 "$ENV_FILE"
printenv | while IFS='=' read -r name value; do
    case "$name" in
        MEM0_*|DIGEST_*|ANTHROPIC_*) ;;
        *) continue ;;
    esac
    # Single-quote the value, escaping any embedded single quotes as '\''.
    escaped=$(printf '%s' "$value" | sed "s/'/'\\\\''/g")
    printf "export %s='%s'\n" "$name" "$escaped" >> "$ENV_FILE"
done

SCHEDULE="${DIGEST_CRON:-0 8 * * *}"
echo "$SCHEDULE . $ENV_FILE; python /app/digest.py >> /var/log/digest.log 2>&1" \
    > /etc/crontabs/root

touch /var/log/digest.log
echo "digest: scheduled '$SCHEDULE' (UTC)." >> /var/log/digest.log

# Optionally run once at startup (handy to verify config right after deploy).
if [ "${DIGEST_RUN_ON_START:-false}" = "true" ]; then
    ( . "$ENV_FILE"; python /app/digest.py ) >> /var/log/digest.log 2>&1 || true
fi

# Surface the digest log through `docker logs` / `caprover logs`.
tail -F /var/log/digest.log &
exec crond -f -l 8
