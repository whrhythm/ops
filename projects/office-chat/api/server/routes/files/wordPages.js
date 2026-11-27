const express = require('express');
const { createHash } = require('node:crypto');
const { Readable } = require('node:stream');
const { pipeline } = require('node:stream/promises');

function wordPagesAvailable(user) {
  const issuer = process.env.OPENID_ISSUER?.replace(/\/$/, '');
  const subject = process.env.STARBRIDGE_WORD_PAGES_SUBJECT;
  return Boolean(process.env.STARBRIDGE_WORD_PAGES_ENABLED === 'true' && issuer && subject &&
    process.env.STARBRIDGE_SCAN_TOKEN && user?.provider === 'openid' &&
    !!user.openidId && (subject === 'all' || user.openidId === subject) &&
    user.openidIssuer?.replace(/\/$/, '') === issuer);
}

function wordPagesRouter() {
  const router = express.Router();
  router.post('/', (req, res, next) => wordPagesAvailable(req.user) ? next() : res.sendStatus(404),
    express.raw({ type: 'application/octet-stream', limit: '16mb' }), async (req, res) => {
      if (!Buffer.isBuffer(req.body) || !req.body.length) return res.sendStatus(400);
      const owner = createHash('sha256').update(JSON.stringify([process.env.OPENID_ISSUER, req.user.openidId])).digest('hex');
      try {
        const endpoint = new URL('/word-pages', process.env.STARBRIDGE_SCAN_URL);
        if (endpoint.protocol !== 'https:') return res.sendStatus(503);
        const response = await fetch(endpoint, { method: 'POST', body: req.body, redirect: 'error',
          signal: AbortSignal.timeout(100000), headers: { Authorization: 'Bearer ' + process.env.STARBRIDGE_SCAN_TOKEN,
            'X-Scan-Owner': owner, 'Content-Type': 'application/octet-stream' } });
        if (!response.ok || !response.body) {
          await response.body?.cancel();
          return res.sendStatus([413, 422, 429].includes(response.status) ? response.status : 503);
        }
        res.set({ 'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
        await pipeline(Readable.fromWeb(response.body), res);
      } catch {
        if (!res.headersSent) res.sendStatus(503);
        else res.destroy();
      }
    });
  return router;
}

module.exports = { wordPagesAvailable, wordPagesRouter };
