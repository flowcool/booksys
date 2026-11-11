FROM ghcr.io/thephaseless/byparr@sha256:c66fa161187b8fa527bfad7eefa2647ea1c553f77df68cbffd6fe83366d1787b
USER root
# Update the two inherited Ubuntu packages with fixable severe findings.
# pip is a build prerequisite only. Remove both inherited pip installations and
# their vulnerable vendored packages after installing the worker dependencies.
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade apt=3.2.1ubuntu0.1 libapt-pkg7.0=3.2.1ubuntu0.1 \
    && rm -rf /var/lib/apt/lists/* \
    && /app/.venv/bin/python -m ensurepip \
    && /app/.venv/bin/python -m pip install --no-cache-dir beautifulsoup4==4.14.3 PyYAML==6.0.3 \
    && runtime_python=$(readlink -f /app/.venv/bin/python) \
    && "$runtime_python" -m pip uninstall --break-system-packages -y pip \
    && /app/.venv/bin/python -m pip uninstall -y pip
WORKDIR /collector
COPY booksys.py pipeline.py fiches.py mirrors.py mirror_routing.py retirement_history.py activity.py ./
ENV PYTHONUNBUFFERED=1
USER 1000:1000
ENTRYPOINT ["/app/.venv/bin/python", "/collector/pipeline.py"]
