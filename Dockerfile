FROM kicad/kicad:10.0.6-full@sha256:e36e29b5f4a638a1a7238eba3d2fcbc8e061ffed1e99a8f39cb722706c94a58e

ARG FREEROUTING_VERSION=2.1.0
ARG FREEROUTING_SHA256=2c07d58f75dac03782664081e7a58b41c25400d871a9fcf166a2ea6fe60d5def

ARG KIKIT_VERSION=1.8.1
ARG PCBDRAW_VERSION=1.3.0
ARG SKIDL_VERSION=2.3.0
ARG WIREVIZ_VERSION=0.4.1
ARG KICOST_VERSION=1.1.21

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        default-jre-headless \
        ghostscript \
        graphviz \
        imagemagick \
        librsvg2-bin \
        ngspice \
        openscad \
        poppler-utils \
        python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/kicad-mcp \
    && /opt/kicad-mcp/bin/pip install --no-cache-dir mcp-server-kicad==0.20.1 \
    && python3 -m venv --system-site-packages /opt/kicad-tools \
    && /opt/kicad-tools/bin/pip install --no-cache-dir \
        "kikit==${KIKIT_VERSION}" \
        "pcbdraw==${PCBDRAW_VERSION}" \
        "skidl==${SKIDL_VERSION}" \
        "wireviz==${WIREVIZ_VERSION}" \
        "kicost==${KICOST_VERSION}" \
    && install -d /opt/freerouting /opt/kicad-home-template/.config/kicad/10.0 \
    && curl -fsSL "https://github.com/freerouting/freerouting/releases/download/v${FREEROUTING_VERSION}/freerouting-${FREEROUTING_VERSION}.jar" -o /opt/freerouting/freerouting.jar \
    && printf '%s  %s\n' "$FREEROUTING_SHA256" /opt/freerouting/freerouting.jar | sha256sum -c - \
    && cp /usr/share/kicad/template/sym-lib-table /usr/share/kicad/template/fp-lib-table /opt/kicad-home-template/.config/kicad/10.0/ \
    && chmod -R a+rX /opt/freerouting /opt/kicad-home-template

ENV PATH="/opt/kicad-mcp/bin:/opt/kicad-tools/bin:${PATH}" \
    HOME=/tmp/kicad-home \
    KICAD_PYTHON=/usr/bin/python3 \
    FREEROUTING_JAR=/opt/freerouting/freerouting.jar
WORKDIR /projects
CMD ["sh", "-lc", "mkdir -p \"$HOME\" && cp -a /opt/kicad-home-template/. \"$HOME\"/ && exec sleep infinity"]
