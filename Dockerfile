FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL https://pkg.labs.nic.cz/gpg -o /usr/share/keyrings/cznic-labs-pkg.gpg \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/keyrings/cznic-labs-pkg.gpg] https://pkg.labs.nic.cz/knot-dns ${VERSION_CODENAME} main" \
        > /etc/apt/sources.list.d/cznic-labs-knot-dns.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends libknot16 knot-dnssecutils \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

EXPOSE 8080
CMD ["uvicorn", "--factory", "knot_api.app:create_app", \
     "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
