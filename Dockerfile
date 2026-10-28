# Labeltool-Server für Docker (z. B. Unraid). Druck läuft über den Druck-Helfer auf dem Windows-PC.
# Bauen:  docker build -t labeltool:latest .
# Mit Windows-Installer (GET /api/helfer/installer), Ordner mit Labeltool-Helfer-Setup.exe:
#         docker build --build-context helfer-installer=installer/dist -t labeltool:latest .
# Ohne diese Angabe enthält das Image keinen Installer (der Server bietet dann das Python-Paket an).

# leerer Standard – wird per --build-context helfer-installer=<Ordner> ersetzt (CI: Artefakt des Jobs "installer")
FROM scratch AS helfer-installer

FROM python:3.13-slim

# Unraid-Standard: nobody (99) / users (100) – passt zu den Rechten unter /mnt/user/appdata
ARG PUID=99
ARG PGID=100

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LABELTOOL_DOCKER=1 \
    LABELTOOL_CONFIG=/config/config.yaml \
    LABELTOOL_HOST=0.0.0.0 \
    LABELTOOL_PORT=8765 \
    LABELTOOL_NO_BROWSER=1 \
    TZ=Europe/Berlin

# tzdata, damit TZ wirkt (Zeitstempel in Status/Backups)
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY labeltool/ labeltool/
# Windows-Skripte für den Helfer-Download (/api/helfer/download)
COPY helfer.bat helfer-autostart.bat ./
COPY docker/config.yaml docker/config.yaml
# Windows-Setup des Druck-Helfers (optional, s. oben) -> /app/helfer-installer/Labeltool-Helfer-Setup.exe
COPY --from=helfer-installer / /app/helfer-installer/

RUN (getent group "${PGID}" >/dev/null || groupadd -g "${PGID}" labeltool) \
 && useradd -u "${PUID}" -g "${PGID}" -M -d /app -s /usr/sbin/nologin labeltool \
 && mkdir -p /config /ausgabe /vorlagen \
 && chown "${PUID}:${PGID}" /config /ausgabe /vorlagen

USER ${PUID}:${PGID}
VOLUME ["/config", "/ausgabe", "/vorlagen"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/meta' % os.environ.get('LABELTOOL_PORT', '8765'), timeout=4)"]

CMD ["python", "-m", "labeltool"]
