const express = require('express');
const { createEnterpriseHandler, isEnabled } = require('@librechat/api');
const { requireJwtAuth, checkBan } = require('~/server/middleware');

const router = express.Router();
if (isEnabled(process.env.STARBRIDGE_ENTERPRISE_ENABLED)) {
  const handler = createEnterpriseHandler({
    url: process.env.STARBRIDGE_FILES_URL,
    issuer: process.env.OPENID_ISSUER,
    serviceToken: process.env.CODECHAT_SERVICE_TOKEN,
    fetch: global.fetch,
  });
  router.use(requireJwtAuth, checkBan);
  router.post('/invitations/:invitationId/accept', handler);
  router.get('/:operation', handler);
  router.post('/:operation', handler);
  router.get('/:enterpriseId/:operation', handler);
  router.post('/:enterpriseId/:operation', handler);
  router.post('/:enterpriseId/invitations/:invitationId/revoke', handler);
  router.get('/:enterpriseId/files/:assetId/versions', handler);
  router.post('/:enterpriseId/files/:assetId/versions', handler);
  router.get('/:enterpriseId/files/:assetId/versions/:number', handler);
}

module.exports = router;
