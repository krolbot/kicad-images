FROM eclipse-temurin:25-jdk@sha256:8c052273471ca5128e67f18ffe9e9c32abf1b104bb0090fa10175eda51004145 AS freerouting-build
ADD --checksum=sha256:f6f51bb02245e8e717f9359bd260cc9c5c0b1bc0acc8b7cb2cd5b8ffeb5de3c7 https://github.com/freerouting/freerouting/releases/download/v2.5.0/freerouting-2.5.0.jar /build/upstream.jar
COPY freerouting/FoundConnectionInserter.java /build/FoundConnectionInserter.java
RUN javac -J-Xmx512m -proc:none -implicit:none --release 25 -cp /build/upstream.jar -d /build/classes /build/FoundConnectionInserter.java

FROM ghcr.io/linuxserver/baseimage-selkies:ubunturesolute@sha256:beebd1f142f792e9767780a70cbc6c41c6c448e12fcda4504417404f1f118df3

ARG KICAD_PACKAGE_VERSION=10.0.8~ubuntu26.04.1

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
        openjdk-25-jre-headless \
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
    && /opt/kicad-tools/bin/pip install --no-cache-dir --no-compile -r /opt/kicad-build/requirements-tools.txt \
    && /opt/kicad-tools/bin/pip check \
    && install -d -o abc -g abc /opt/kicad-build/probe-home \
    && /command/s6-setuidgid abc env HOME=/opt/kicad-build/probe-home /opt/kicad-tools/bin/kibot --help-output gerber >/dev/null \
    && /opt/kicad-tools/bin/python -c 'import os, sysconfig; os.symlink(sysconfig.get_path("purelib"), "/opt/kicad-tools/site-packages")' \
    && /usr/bin/python3 -m venv /opt/kicad-mcp \
    && /opt/kicad-mcp/bin/pip install --no-cache-dir -r /opt/kicad-build/requirements-mcp.txt \
    && /opt/kicad-mcp/bin/pip check \
    && install -d /opt/freerouting \
    && chmod -R a+rX /opt/kicad-tools /opt/kicad-mcp /opt/freerouting

COPY --from=freerouting-build /build/upstream.jar /opt/freerouting/freerouting.jar
COPY --from=freerouting-build /build/classes/ /opt/kicad-build/freerouting-classes/
COPY freerouting/patch_jar.py /opt/kicad-build/patch_freerouting_jar.py
RUN /usr/bin/python3 /opt/kicad-build/patch_freerouting_jar.py /opt/freerouting/freerouting.jar /opt/kicad-build/freerouting-classes
LABEL dev.nlab.freerouting.base="2.5.0" \
      dev.nlab.freerouting.fix="b51633f0ccfe9ddd921ef55fa709d673d101c14e"

COPY patches/mcp-write-permission.patch patches/mcp-autoroute.patch patches/mcp-bounded-pipeline.patch /opt/kicad-build/
RUN cd "$(/opt/kicad-mcp/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')" \
    && git apply --check /opt/kicad-build/mcp-write-permission.patch \
    && git apply /opt/kicad-build/mcp-write-permission.patch \
    && git apply --check /opt/kicad-build/mcp-autoroute.patch \
    && git apply /opt/kicad-build/mcp-autoroute.patch \
    && git apply --check /opt/kicad-build/mcp-bounded-pipeline.patch \
    && git apply /opt/kicad-build/mcp-bounded-pipeline.patch
COPY patches/parts-placer-kicad10.patch /opt/kicad-build/
COPY pcm.py addons.lock.json requirements-ipc.txt /usr/local/share/kicad/
RUN /usr/bin/python3 /usr/local/share/kicad/pcm.py build /usr/local/share/kicad/addons.lock.json /opt/kicad-addons \
    && git -C /opt/kicad-addons apply --check /opt/kicad-build/parts-placer-kicad10.patch \
    && git -C /opt/kicad-addons apply /opt/kicad-build/parts-placer-kicad10.patch \
    && /usr/bin/python3 /usr/local/share/kicad/pcm.py runtimes /usr/local/share/kicad/addons.lock.json /opt/kicad-addons \
    && chmod -R a+rX /opt/kicad-addons
ADD --checksum=sha256:19e862b8e38d06e36c2bb73e679cebe93f45a174b189fd8f3d467ef8dc72d1f1 https://raw.githubusercontent.com/MitjaNemec/ReplicateLayout/52fb33c26e79a2b5e0735dd96e7a2c00f1b5ea66/replicate_layout_test_project/cascade.kicad_sch /opt/kicad-build/replication-fixture/cascade.kicad_sch
ADD --checksum=sha256:00eda82491b90981f2e95b79bae508c9053786445995dfbe98a011542beb7bea https://raw.githubusercontent.com/MitjaNemec/ReplicateLayout/52fb33c26e79a2b5e0735dd96e7a2c00f1b5ea66/replicate_layout_test_project/low_pass.kicad_sch /opt/kicad-build/replication-fixture/low_pass.kicad_sch
ADD --checksum=sha256:e8a308874ba94cd1cf43a580f7436a3b0de7bbd08502118472283a1d57985b2e https://raw.githubusercontent.com/MitjaNemec/ReplicateLayout/52fb33c26e79a2b5e0735dd96e7a2c00f1b5ea66/replicate_layout_test_project/replicate_layout_test_project.kicad_pcb /opt/kicad-build/replication-fixture/replicate_layout_test_project.kicad_pcb
ADD --checksum=sha256:ca46e7bc98e93d7885cf572651bcf20d28fc4219805e77527ac1adaa1eb756de https://raw.githubusercontent.com/MitjaNemec/ReplicateLayout/52fb33c26e79a2b5e0735dd96e7a2c00f1b5ea66/replicate_layout_test_project/replicate_layout_test_project.kicad_pro /opt/kicad-build/replication-fixture/replicate_layout_test_project.kicad_pro
ADD --checksum=sha256:bd067cbf8b4726d726ee0c8beb484eac44a0cda514b46385ee6b100dea58fa08 https://raw.githubusercontent.com/MitjaNemec/ReplicateLayout/52fb33c26e79a2b5e0735dd96e7a2c00f1b5ea66/replicate_layout_test_project/replicate_layout_test_project.kicad_sch /opt/kicad-build/replication-fixture/replicate_layout_test_project.kicad_sch
ADD --checksum=sha256:7a6ecfc7da6127b914e1b779b3ffac5479fde66e992ed1c55cfa7774f9ae4243 https://raw.githubusercontent.com/MitjaNemec/ReplicateLayout/52fb33c26e79a2b5e0735dd96e7a2c00f1b5ea66/test_helpers.py /opt/kicad-build/replication-fixture/test_helpers.py
COPY mcp_smoke.py mcp_autoroute_smoke.py toolkit_mcp.py toolkit_native.py mcp_toolkit_smoke.py native_ipc_smoke.py /usr/local/share/kicad/
COPY root/ /
RUN chmod 0755 /usr/local/bin/kicad-mcp /usr/local/bin/freerouting /usr/local/bin/ibom /custom-cont-init.d/50-kicad-toolkit.sh \
    && chmod -R a+rX /opt/kicad-build/replication-fixture \
    && ln -sf /usr/local/bin/kicad-mcp /opt/kicad-mcp/bin/mcp-server-kicad

ENV PATH="/opt/kicad-tools/bin:/opt/kicad-mcp/bin:${PATH}" \
    PYTHONPATH="/opt/kicad-tools/site-packages" \
    KICAD_VERSION=10.0.8 \
    KICAD_DOCUMENTS_HOME=/config/.local/share \
    KICAD_PYTHON=/usr/bin/python3 \
    FREEROUTING_JAR=/opt/freerouting/freerouting.jar

RUN install -d -o abc -g abc /projects /opt/kicad-build/probe-home \
    && export HOME=/opt/kicad-build/probe-home \
        XDG_CONFIG_HOME=/opt/kicad-build/probe-home/.config \
        XDG_CACHE_HOME=/opt/kicad-build/probe-home/.cache \
        KICAD_CONFIG_HOME=/opt/kicad-build/probe-home/.config/kicad \
        KICAD_DOCUMENTS_HOME=/opt/kicad-build/probe-home/.local/share \
    && /command/s6-setuidgid abc /usr/bin/python3 /usr/local/share/kicad/pcm.py seed /opt/kicad-addons "$HOME" \
    && /command/s6-setuidgid abc env -u PYTHONPATH /opt/kicad-mcp/bin/python /usr/local/share/kicad/mcp_smoke.py \
    && /command/s6-setuidgid abc env -u PYTHONPATH /opt/kicad-mcp/bin/python /usr/local/share/kicad/mcp_autoroute_smoke.py \
    && /command/s6-setuidgid abc env -u PYTHONPATH /opt/kicad-mcp/bin/python /usr/local/share/kicad/mcp_toolkit_smoke.py \
    && rm -rf /opt/kicad-build/probe-home \
    && /usr/bin/python3 -c 'from pathlib import Path; import shutil; paths=list(map(Path, ["/config/.config/kicad", "/config/.cache", "/config/.local/share/kicad"])); assert not any(p.is_symlink() for p in paths); [shutil.rmtree(p) for p in paths if p.exists()]'

# A fresh RUN gives Xvfb a clean process/socket namespace after the HTML BOM probe.
RUN /command/s6-setuidgid abc env -u PYTHONPATH timeout 120s xvfb-run -a -e /dev/stderr /opt/kicad-mcp/bin/python -u /usr/local/share/kicad/native_ipc_smoke.py

# The inherited /init starts the browser desktop; MCP is invoked via docker exec.
WORKDIR /projects
