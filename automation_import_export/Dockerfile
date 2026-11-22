# Automation Import/Export Manager App. Built on the device by the Supervisor (config.yaml has no `image:`).
# The base image is named explicitly (Supervisor 2026.04+) and pinned for reproducible builds.
FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.11.0

ARG BUILD_VERSION=0.1.1
ARG BUILD_ARCH=amd64

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/opt/aie

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore --only-binary=:all: \
        -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# The backend (aie) and its web interface (aie/static).
COPY backend/ /opt/aie/
COPY rootfs /
RUN chmod a+x /etc/s6-overlay/s6-rc.d/aie/run /etc/s6-overlay/s6-rc.d/aie/finish

LABEL \
    io.hass.name="Automation Import/Export Manager" \
    io.hass.description="Export, archive, clean up and restore Home Assistant automations with their metadata" \
    io.hass.url="https://github.com/havanissa/HomeAssistant/tree/main/automation_import_export" \
    io.hass.type="app" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.arch="${BUILD_ARCH}"
