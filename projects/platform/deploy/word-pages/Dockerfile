FROM ubuntu:22.04@sha256:f64b2ffc2f10ab8f8505aa56b2664bd846cbb9df87c7e2532e30f7667021c0e6
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
COPY mozilla-ca.crt /tmp/mozilla-ca.crt
RUN sed -i -E 's#http://(archive|security)\.ubuntu\.com/ubuntu#https://mirrors.aliyun.com/ubuntu#g' /etc/apt/sources.list && \
    apt-get -o Acquire::https::CaInfo=/tmp/mozilla-ca.crt -o Acquire::https::Timeout=20 update && \
    apt-get -o Acquire::https::CaInfo=/tmp/mozilla-ca.crt -o Acquire::https::Timeout=20 install -y --no-install-recommends \
    python3 python3-lxml libreoffice-writer libreoffice-math poppler-utils fonts-noto-cjk fonts-crosextra-carlito \
    fonts-liberation2 fonts-opensymbol && rm -rf /var/lib/apt/lists/* /tmp/mozilla-ca.crt
COPY font-aliases.conf /etc/fonts/conf.d/99-starbridge-word.conf
COPY server.py font_metrics.py /opt/word-pages/
USER 10002:10002
WORKDIR /opt/word-pages
ENTRYPOINT ["python3", "-Es", "server.py"]
