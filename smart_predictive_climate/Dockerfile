# Smart Adaptive & Predictive Climate App. Built on the device by the Supervisor (config.yaml has no `image:`).
# The base image is named explicitly (Supervisor 2026.04+) and pinned for reproducible builds.
FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.11.0

ARG BUILD_VERSION=0.1.0
ARG BUILD_ARCH=amd64

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/opt/spc

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --root-user-action=ignore --only-binary=:all: \
        -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# The backend (spc) and its web interface (spc/static).
COPY backend/ /opt/spc/
COPY rootfs /
RUN chmod a+x /etc/s6-overlay/s6-rc.d/spc/run /etc/s6-overlay/s6-rc.d/spc/finish

LABEL \
    io.hass.name="Smart Adaptive & Predictive Climate" \
    io.hass.description="Learns and predicts room temperatures; observes and recommends, never controls a device" \
    io.hass.url="https://github.com/havanissa/HomeAssistant/tree/main/smart_predictive_climate" \
    io.hass.type="app" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.arch="${BUILD_ARCH}"
