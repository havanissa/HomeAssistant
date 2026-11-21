# Smart Occupancy Detection App. Built on the device by the Supervisor (config.yaml has no `image:`).
# The base image is named explicitly (Supervisor 2026.04+) and pinned for reproducible builds.
FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.11.0

ARG BUILD_VERSION=0.1.0
ARG BUILD_ARCH=amd64

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/opt/sod

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore --only-binary=:all: \
        -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# The engine (sod_engine), the backend (sod) and its web interface (sod/static).
COPY backend/ /opt/sod/
COPY rootfs /
RUN chmod a+x /etc/s6-overlay/s6-rc.d/sod/run /etc/s6-overlay/s6-rc.d/sod/finish

LABEL \
    io.hass.name="Smart Occupancy Detection" \
    io.hass.description="Room occupancy detection, published as native Home Assistant entities" \
    io.hass.url="https://github.com/havanissa/HomeAssistant/tree/main/smart_occupancy_detection" \
    io.hass.type="app" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.arch="${BUILD_ARCH}"
