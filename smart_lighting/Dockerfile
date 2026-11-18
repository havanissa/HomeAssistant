# Smart Occupancy & Lighting App.
# Built on the device by the Supervisor (config.yaml has no `image:`).
# Since Supervisor 2026.04 the base image must be named here explicitly;
# it is pinned for reproducible builds.
FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.11.0

ARG BUILD_VERSION=0.5.0
ARG BUILD_ARCH=amd64

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/opt/smart_lighting

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore --only-binary=:all: \
        -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Backend package plus the prebuilt web interface (backend/smart_lighting/static).
COPY backend/ /opt/smart_lighting/
COPY rootfs /
RUN chmod a+x /etc/s6-overlay/s6-rc.d/smart-lighting/run \
              /etc/s6-overlay/s6-rc.d/smart-lighting/finish

LABEL \
    io.hass.name="Smart Occupancy & Lighting" \
    io.hass.description="Web interface for Smart Occupancy & Lighting" \
    io.hass.url="https://github.com/havanissa/HomeAssistant/tree/main/smart_lighting" \
    io.hass.type="app" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.arch="${BUILD_ARCH}"
