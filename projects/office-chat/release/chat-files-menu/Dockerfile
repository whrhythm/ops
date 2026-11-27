FROM harbor.k3s.local/internal/starbridge-office@sha256:f7bf28f75e7136135a05d582a2cb2193ed13b1b615a3e196fbf85a38e6e9c777
USER 0
COPY install.cjs translation.json /tmp/chat-files-ui/
RUN node /tmp/chat-files-ui/install.cjs && node --check /app/client/dist/assets/index.DJVdngRf.officeui7.word2.reader2.chatfiles1.js
USER 1000:1000
