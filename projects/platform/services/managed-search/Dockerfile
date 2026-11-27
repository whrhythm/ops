FROM node:24-alpine
WORKDIR /app
COPY *.cjs *.json ./
USER node
EXPOSE 8080
CMD ["node","server.cjs"]
