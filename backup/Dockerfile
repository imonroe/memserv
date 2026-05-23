FROM alpine:3.20

# coreutils provides GNU `date`, which supports `date -d "N days ago"`
# (BusyBox's date does not) used for S3 retention pruning in backup.sh.
RUN apk add --no-cache curl jq aws-cli dcron tzdata bash coreutils \
    && cp /usr/share/zoneinfo/UTC /etc/localtime

COPY backup.sh /usr/local/bin/backup.sh
COPY crontab /etc/crontabs/root
RUN chmod +x /usr/local/bin/backup.sh

CMD ["crond", "-f", "-l", "8"]
