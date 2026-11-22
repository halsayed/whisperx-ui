# WhisperX Pipeline UI: FastAPI app + kubectl in one small image.
FROM python:3.12-slim

ARG KUBECTL_VERSION=v1.36.2
ARG TARGETARCH=amd64
ADD --chmod=755 https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl /usr/local/bin/kubectl

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && kubectl version --client

COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--no-server-header"]
