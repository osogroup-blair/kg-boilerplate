FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi==0.143.0" "uvicorn==0.54.0" "pydantic==2.14.0" "tiktoken==0.14.0"
COPY packages/common/src/kg_common /app/kg_common
COPY tools/__init__.py /app/tools/__init__.py
COPY tools/mock_router /app/tools/mock_router
# Pre-fetch the tokenizer BPE so the container works offline afterwards.
RUN python -c "import tiktoken; tiktoken.get_encoding('o200k_base')"
RUN useradd --system --uid 10001 --no-create-home router
USER 10001
EXPOSE 8781
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8781/health', timeout=2)" || exit 1
CMD ["uvicorn", "tools.mock_router.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8781"]
