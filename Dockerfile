# syntax=docker/dockerfile:1
#
# ZEROHERO dynamic control — container image for NAS deployment.
#
# python:3.12-slim is multi-arch, so this builds unchanged on an x86 Synology or
# an ARM QNAP. Every dependency is pure Python, so there is no compiler to
# install and no wheel to build.

FROM python:3.12-slim AS base

# PYTHONUNBUFFERED so logs reach `docker logs` immediately rather than sitting in
# a pipe buffer — with one decision a day, a buffered log is a useless log.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Australia/Sydney

WORKDIR /app

# tzdata is NOT optional. Everything in this app is defined in local wall-clock
# time — the 18:00-21:00 credit window, the 11:00-14:00 free window, the AEST/AEDT
# changeover — and python:slim ships no zone database, so ZoneInfo("Australia/Sydney")
# would raise at startup. The pip package is used rather than apt so the image
# stays slim and the zone data updates with a normal dependency bump.
COPY pyproject.toml README.md ./
COPY zerohero_dynamic_control ./zerohero_dynamic_control
RUN pip install --no-cache-dir '.[forecast,api]' tzdata

# Run unprivileged. UID 1000 matches the first user on most NAS platforms, which
# keeps the bind-mounted ./var writable without a chown. Override with `user:` in
# compose if your NAS numbers users differently (Synology often starts at 1026).
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin zerohero \
    && mkdir -p /app/var && chown -R zerohero:zerohero /app
USER zerohero

VOLUME ["/app/var"]
EXPOSE 8787

# The API is the liveness signal: it is served by the same event loop that runs
# the scheduler, so if the loop wedges the healthcheck fails and the NAS restarts
# the container. urllib is used rather than curl, which slim does not ship.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys;\
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/status', timeout=8).status==200 else 1)"]

# SIGTERM must reach Python so the inverter is restored before exit — see
# ZeroHeroScheduler._install_signal_handlers. Exec form (no shell) guarantees
# the process is PID 1 and receives the signal directly.
STOPSIGNAL SIGTERM
ENTRYPOINT ["zerohero"]
CMD ["serve", "--host", "0.0.0.0", "--log-level", "INFO"]
