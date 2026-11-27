FROM node:22 AS deps
WORKDIR /deps
COPY deps/package*.json ./
RUN npm ci --ignore-scripts
FROM harbor.k3s.local/internal/starbridge-office@sha256:a3324c2e4b06396a622a594692c00f3d1603386870e03d9985f9f5f67a0f9746
USER 0
COPY --from=deps --chown=1000:1000 /deps/node_modules/pdfjs-dist /opt/starbridge-pdf/node_modules/pdfjs-dist
COPY --chown=1000:1000 pdf /opt/starbridge-pdf
WORKDIR /opt/starbridge-pdf
USER 1000:1000
EXPOSE 3001
ENTRYPOINT ["node","/opt/starbridge-pdf/index.js"]
CMD ["--enable-interact","/input","/output"]
