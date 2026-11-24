FROM rustdesk/rustdesk-server:latest AS rustdesk-bin

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        libstdc++6 \
        python3 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=rustdesk-bin /usr/bin/hbbs /usr/local/bin/hbbs
COPY --from=rustdesk-bin /usr/bin/hbbr /usr/local/bin/hbbr
COPY panel /opt/flixdesk-panel

WORKDIR /data

ENTRYPOINT ["python3", "/opt/flixdesk-panel/panel.py"]
