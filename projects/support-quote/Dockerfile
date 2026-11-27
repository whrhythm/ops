FROM harbor.k3s.local/codechat-base/python@sha256:914752997e0f5fb7c78351fefefb378b7d1a2c3cad7d88ebe4b195dd5751f5ff
COPY quote.py /opt/support-quote/quote.py
USER 65532:65532
ENTRYPOINT ["python", "-B", "/opt/support-quote/quote.py"]
