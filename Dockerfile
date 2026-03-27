FROM python:3.12-slim AS builder

WORKDIR /build
COPY . .
RUN pip install --user --no-cache-dir .

FROM python:3.12-slim

WORKDIR /app
RUN groupadd -r zbbx && useradd -r -g zbbx -d /app zbbx
COPY --from=builder /root/.local /home/zbbx/.local
ENV PATH=/home/zbbx/.local/bin:$PATH

USER zbbx

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import zbbx_mcp" || exit 1

ENTRYPOINT ["zbbx-mcp"]
CMD ["--transport", "sse", "--port", "8000"]
