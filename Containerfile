# wikijs-search-shim — base image + one file, nothing else.
# Pin by digest for reproducible builds once you can resolve one:
#   podman pull docker.io/library/python:3.13-slim
#   podman image inspect --format '{{index .RepoDigests 0}}' python:3.13-slim
FROM docker.io/library/python:3.13-slim

# No RUN pip install, no apt-get. Stdlib only by design.

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin shim

COPY --chmod=0644 wikijs-search-shim.py /app/wikijs-search-shim.py

USER 10001

# Loopback default in the script is right for bare metal, wrong for a container.
ENV LISTEN_ADDR=0.0.0.0 \
    LISTEN_PORT=8099 \
    PYTHONUNBUFFERED=1

EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/healthz',timeout=3).status==200 else 1)"

ENTRYPOINT ["python3", "/app/wikijs-search-shim.py"]
