FROM alpine:3.20

RUN apk add --no-cache curl jq aws-cli dcron tzdata bash \
    && cp /usr/share/zoneinfo/UTC /etc/localtime

COPY backup.sh /usr/local/bin/backup.sh
COPY crontab /etc/crontabs/root
RUN chmod +x /usr/local/bin/backup.sh

CMD ["crond", "-f", "-l", "8"]
