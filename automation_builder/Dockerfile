# Automation Builder App. Built on the device by the Supervisor (config.yaml has no `image:`).
# The base image is named explicitly (Supervisor 2026.04+) and pinned for reproducible builds.
FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.11.0

ARG BUILD_VERSION=0.2.0
ARG BUILD_ARCH=amd64

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/opt/ab

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore --only-binary=:all: \
        -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# The backend (ab) and its web interface (ab/static).
COPY backend/ /opt/ab/
COPY rootfs /
RUN chmod a+x /etc/s6-overlay/s6-rc.d/ab/run /etc/s6-overlay/s6-rc.d/ab/finish

LABEL \
    io.hass.name="Automation Builder" \
    io.hass.description="Visual IFTTT rules per device, compiled into native Home Assistant automations" \
    io.hass.url="https://github.com/havanissa/HomeAssistant/tree/main/automation_builder" \
    io.hass.type="app" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.arch="${BUILD_ARCH}"
