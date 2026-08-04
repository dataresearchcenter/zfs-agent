FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive
# zfsutils-linux is in the contrib section; the trixie-backports build
# (2.4.x) is needed to match the host's 2.4 kernel module interface.
RUN sed -i 's/^Components: main$/Components: main contrib/' \
    /etc/apt/sources.list.d/debian.sources \
    && printf 'Types: deb\nURIs: http://deb.debian.org/debian\nSuites: trixie-backports\nComponents: main contrib\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n' \
    > /etc/apt/sources.list.d/backports.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    -t trixie-backports zfsutils-linux \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 1000 --create-home appuser

COPY . /src
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir /src \
    && chmod +x /src/tests/docker/entrypoint.sh

ENTRYPOINT ["/src/tests/docker/entrypoint.sh"]
