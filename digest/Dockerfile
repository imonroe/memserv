FROM python:3.12-alpine

# dcron provides crond; tzdata + UTC keeps the schedule unambiguous.
RUN apk add --no-cache dcron tzdata \
    && cp /usr/share/zoneinfo/UTC /etc/localtime

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY digest.py .
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

CMD ["/usr/local/bin/entrypoint.sh"]
