#!/bin/bash
set -euo pipefail

: "${QDRANT_URL:?required}"        # e.g. https://qdrant.your-domain.com
: "${QDRANT_API_KEY:?required}"
: "${MEM0_COLLECTION:?required}"
: "${S3_BUCKET:?required}"
: "${S3_PREFIX:=mem0-backups}"
: "${AWS_ACCESS_KEY_ID:?required}"
: "${AWS_SECRET_ACCESS_KEY:?required}"
: "${AWS_DEFAULT_REGION:=us-east-1}"
: "${RETENTION_DAYS:=14}"

TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)
LOCAL_DIR=/tmp/snapshots
mkdir -p "$LOCAL_DIR"

echo "[$TS] Creating snapshot of $MEM0_COLLECTION..."
SNAPSHOT_NAME=$(
  curl -fsSL -X POST \
    -H "api-key: $QDRANT_API_KEY" \
    "$QDRANT_URL/collections/$MEM0_COLLECTION/snapshots" \
  | jq -r '.result.name'
)

echo "[$TS] Snapshot created: $SNAPSHOT_NAME"

LOCAL_FILE="$LOCAL_DIR/${TS}_${SNAPSHOT_NAME}"
curl -fsSL \
  -H "api-key: $QDRANT_API_KEY" \
  "$QDRANT_URL/collections/$MEM0_COLLECTION/snapshots/$SNAPSHOT_NAME" \
  -o "$LOCAL_FILE"

SIZE=$(stat -c%s "$LOCAL_FILE")
echo "[$TS] Downloaded $SIZE bytes to $LOCAL_FILE"

echo "[$TS] Uploading to s3://$S3_BUCKET/$S3_PREFIX/"
aws s3 cp "$LOCAL_FILE" "s3://$S3_BUCKET/$S3_PREFIX/${TS}.snapshot"

# Delete the Qdrant-side snapshot to free space on the droplet
curl -fsSL -X DELETE \
  -H "api-key: $QDRANT_API_KEY" \
  "$QDRANT_URL/collections/$MEM0_COLLECTION/snapshots/$SNAPSHOT_NAME" > /dev/null

# Local rotation: keep 3 most recent
ls -1t "$LOCAL_DIR" | tail -n +4 | xargs -I {} rm -f "$LOCAL_DIR/{}"

# S3 rotation: delete objects older than RETENTION_DAYS.
# BusyBox date on Alpine does not support GNU relative forms like
# `date -d "14 days ago"`, so compute the cutoff with epoch math first.
CUTOFF_EPOCH=$(( $(date -u +%s) - RETENTION_DAYS * 86400 ))
CUTOFF=$(date -u -D %s -d "$CUTOFF_EPOCH" +%Y-%m-%d)
aws s3 ls "s3://$S3_BUCKET/$S3_PREFIX/" \
  | awk '{print $4}' \
  | while read -r KEY; do
    KEY_DATE=$(echo "$KEY" | cut -c1-10)
    if [[ "$KEY_DATE" < "$CUTOFF" ]]; then
      echo "[$TS] Pruning s3://$S3_BUCKET/$S3_PREFIX/$KEY"
      aws s3 rm "s3://$S3_BUCKET/$S3_PREFIX/$KEY"
    fi
  done

echo "[$TS] Backup complete."
