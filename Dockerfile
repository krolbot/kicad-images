FROM linuxserver/kicad:10.0.3-r0-ls153@sha256:041619c60f795effe4b96c23788edcbd63fb12fad4b8267e6f06372671b99641

ARG FREEROUTING_VERSION=2.1.0
ARG FREEROUTING_SHA256=2c07d58f75dac03782664081e7a58b41c25400d871a9fcf166a2ea6fe60d5def

# LinuxServer /init needs root; desktop and MCP operations use abc.
# hadolint ignore=DL3002
USER 0
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]
COPY requirements-tools.txt requirements-mcp.txt /opt/kicad-build/
RUN apk add --no-cache \
        ca-certificates \
        curl \
        font-dejavu \
        ghostscript \
        git \
        graphviz \
        imagemagick \
        imagemagick-svg \
        inkscape \
        librsvg \
        mupdf-tools \
        ngspice \
        openjdk21-jre-headless \
        openscad \
        poppler-utils \
        py3-cairo \
        py3-lxml \
        py3-numpy \
        py3-pip \
        py3-psutil \
        py3-pyside6 \
        py3-reportlab \
        py3-scipy \
        py3-shapely \
        py3-virtualenv \
        py3-zstd \
        xauth \
        xdotool \
        xvfb \
        xvfb-run \
        zip \
    && python3 -m venv --system-site-packages /opt/kicad-tools \
    && /opt/kicad-tools/bin/pip install --no-cache-dir -r /opt/kicad-build/requirements-tools.txt \
    && /opt/kicad-tools/bin/pip check \
    && /opt/kicad-tools/bin/python -c 'import os, sysconfig; os.symlink(sysconfig.get_path("purelib"), "/opt/kicad-tools/site-packages")' \
    && python3 -m venv /opt/kicad-mcp \
    && /opt/kicad-mcp/bin/pip install --no-cache-dir -r /opt/kicad-build/requirements-mcp.txt \
    && /opt/kicad-mcp/bin/pip check \
    && install -d /opt/freerouting \
    && curl -fsSL "https://github.com/freerouting/freerouting/releases/download/v${FREEROUTING_VERSION}/freerouting-${FREEROUTING_VERSION}.jar" -o /opt/freerouting/freerouting.jar \
    && printf '%s  %s\n' "$FREEROUTING_SHA256" /opt/freerouting/freerouting.jar | sha256sum -c - \
    && chmod -R a+rX /opt/kicad-tools /opt/kicad-mcp /opt/freerouting

COPY pcm.py addons.lock.json mcp_smoke.py /usr/local/share/kicad/
RUN python3 /usr/local/share/kicad/pcm.py build /usr/local/share/kicad/addons.lock.json /opt/kicad-addons \
    && python3 /usr/local/share/kicad/pcm.py runtimes /usr/local/share/kicad/addons.lock.json /opt/kicad-addons \
    && chmod -R a+rX /opt/kicad-addons
COPY root/ /
RUN chmod 0755 /usr/local/bin/kicad-mcp /usr/local/bin/freerouting /usr/local/bin/ibom /custom-cont-init.d/50-kicad-toolkit.sh

ENV PATH="/opt/kicad-tools/bin:/opt/kicad-mcp/bin:${PATH}" \
    PYTHONPATH="/opt/kicad-tools/site-packages" \
    KICAD_VERSION=10.0.3 \
    KICAD_DOCUMENTS_HOME=/config/.local/share \
    KICAD_PYTHON=/usr/bin/python3 \
    FREEROUTING_JAR=/opt/freerouting/freerouting.jar

RUN env -u PYTHONPATH /opt/kicad-mcp/bin/python /usr/local/share/kicad/mcp_smoke.py

# The inherited /init starts the browser desktop; MCP is invoked via docker exec.
WORKDIR /projects
