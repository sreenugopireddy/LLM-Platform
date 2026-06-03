FROM python:3.12-slim AS builder
WORKDIR /build
COPY services/prompt_registry/requirements.txt requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
WORKDIR /app
RUN addgroup --system app && adduser --system --ingroup app app
COPY --from=builder /install /usr/local
COPY services/prompt_registry/ ./services/prompt_registry/
COPY shared/ ./shared/
RUN touch __init__.py services/__init__.py
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
USER app
EXPOSE 8002
CMD ["python", "-m", "uvicorn", "services.prompt_registry.main:app", "--host", "0.0.0.0", "--port", "8002"]