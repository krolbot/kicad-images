FROM ghcr.io/linuxserver/baseimage-selkies:ubunturesolute@sha256:beebd1f142f792e9767780a70cbc6c41c6c448e12fcda4504417404f1f118df3

ARG KICAD_PACKAGE_VERSION=10.0.7~ubuntu26.04.1
ARG FREEROUTING_VERSION=2.1.0
ARG FREEROUTING_SHA256=2c07d58f75dac03782664081e7a58b41c25400d871a9fcf166a2ea6fe60d5def

# LinuxServer /init needs root; desktop and MCP operations use abc.
# hadolint ignore=DL3002
USER 0
SHELL ["/bin/bash", "-eo", "pipefail", "-c"]
ENV TITLE=KiCad \
    NO_GAMEPAD=true \
    PIXELFLUX_WAYLAND=false \
    START_DOCKER=false
COPY apt/ /etc/apt/
COPY requirements-tools.txt requirements-mcp.txt /opt/kicad-build/
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        "kicad=${KICAD_PACKAGE_VERSION}" \
        "kicad-footprints=${KICAD_PACKAGE_VERSION}" \
        "kicad-symbols=${KICAD_PACKAGE_VERSION}" \
        "kicad-packages3d=${KICAD_PACKAGE_VERSION}" \
        "kicad-templates=${KICAD_PACKAGE_VERSION}" \
        ca-certificates \
        curl \
        fonts-dejavu-core \
        ghostscript \
        git \
        graphviz \
        imagemagick \
        inkscape \
        libngspice0 \
        librsvg2-bin \
        libxcb-cursor0 \
        mupdf-tools \
        ngspice \
        openjdk-21-jre-headless \
        openscad \
        poppler-utils \
        python3-cairo \
        python3-lxml \
        python3-numpy \
        python3-pip \
        python3-psutil \
        python3-reportlab \
        python3-scipy \
        python3-shapely \
        python3-venv \
        python3-wxgtk4.0 \
        python3-zstandard \
        xauth \
        xdotool \
        xvfb \
        zip \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/bin/python3 -m venv --system-site-packages /opt/kicad-tools \
    && /opt/kicad-tools/bin/pip install --no-cache-dir -r /opt/kicad-build/requirements-tools.txt \
    && /opt/kicad-tools/bin/pip check \
    && /opt/kicad-tools/bin/python -c 'import os, sysconfig; os.symlink(sysconfig.get_path("purelib"), "/opt/kicad-tools/site-packages")' \
    && /usr/bin/python3 -m venv /opt/kicad-mcp \
    && /opt/kicad-mcp/bin/pip install --no-cache-dir -r /opt/kicad-build/requirements-mcp.txt \
    && /opt/kicad-mcp/bin/pip check \
    && install -d /opt/freerouting \
    && curl -fsSL "https://github.com/freerouting/freerouting/releases/download/v${FREEROUTING_VERSION}/freerouting-${FREEROUTING_VERSION}.jar" -o /opt/freerouting/freerouting.jar \
    && printf '%s  %s\n' "$FREEROUTING_SHA256" /opt/freerouting/freerouting.jar | sha256sum -c - \
    && chmod -R a+rX /opt/kicad-tools /opt/kicad-mcp /opt/freerouting

COPY pcm.py addons.lock.json requirements-ipc.txt mcp_smoke.py /usr/local/share/kicad/
RUN /usr/bin/python3 /usr/local/share/kicad/pcm.py build /usr/local/share/kicad/addons.lock.json /opt/kicad-addons \
    && /usr/bin/python3 /usr/local/share/kicad/pcm.py runtimes /usr/local/share/kicad/addons.lock.json /opt/kicad-addons \
    && chmod -R a+rX /opt/kicad-addons
COPY root/ /
RUN chmod 0755 /usr/local/bin/kicad-mcp /usr/local/bin/freerouting /usr/local/bin/ibom /custom-cont-init.d/50-kicad-toolkit.sh

ENV PATH="/opt/kicad-tools/bin:/opt/kicad-mcp/bin:${PATH}" \
    PYTHONPATH="/opt/kicad-tools/site-packages" \
    KICAD_VERSION=10.0.7 \
    KICAD_DOCUMENTS_HOME=/config/.local/share \
    KICAD_PYTHON=/usr/bin/python3 \
    FREEROUTING_JAR=/opt/freerouting/freerouting.jar

RUN env -u PYTHONPATH /opt/kicad-mcp/bin/python /usr/local/share/kicad/mcp_smoke.py

# The inherited /init starts the browser desktop; MCP is invoked via docker exec.
WORKDIR /projects
