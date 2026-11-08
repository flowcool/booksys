FROM ghcr.io/thephaseless/byparr@sha256:c66fa161187b8fa527bfad7eefa2647ea1c553f77df68cbffd6fe83366d1787b
USER root
RUN /app/.venv/bin/python -m ensurepip && /app/.venv/bin/python -m pip install --no-cache-dir beautifulsoup4==4.14.3 PyYAML==6.0.3
WORKDIR /collector
COPY booksys.py pipeline.py fiches.py mirrors.py mirror_routing.py ./
ENV PYTHONUNBUFFERED=1
USER 1000:1000
ENTRYPOINT ["/app/.venv/bin/python", "/collector/pipeline.py"]
