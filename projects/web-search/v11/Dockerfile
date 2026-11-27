FROM harbor.k3s.local/internal/starbridge-project-web@sha256:7f060d5e28c8e8217cdfba25d52c90836e2c3baa3e9792453661ca75361ee9af
WORKDIR /tool
# Same pinned dependencies as v10; only the source reader changes.
COPY app.py .
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--limit-concurrency", "8"]
