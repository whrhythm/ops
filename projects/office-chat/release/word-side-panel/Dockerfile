FROM harbor.k3s.local/internal/starbridge-office@sha256:c27cb2dccebbec887d06fe6468902e67a3356f5f9aef1c3b287fd5f445988775
USER 0
COPY install.cjs reader.js picker.js /tmp/word-side-preview/
RUN node /tmp/word-side-preview/install.cjs && node --check /app/client/dist/assets/index.DJVdngRf.officeui7.word2.reader2.chatfiles1.wordpanel1.js
USER 1000:1000
