FROM docker:28-cli
RUN apk add --no-cache python3 ca-certificates
WORKDIR /app
COPY tmm.py /app/tmm.py
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python3", "/app/tmm.py"]
