const express = require('express');
const {
  createSetBalanceConfig,
  createManagedUsageHandler,
  getCodeApiAuthHeaders,
} = require('@librechat/api');
const router = express.Router();
const controller = require('../controllers/Balance');
const { requireJwtAuth } = require('../middleware/');
const { findBalanceByUser, upsertBalanceFields } = require('~/models');
const { getAppConfig } = require('~/server/services/Config');

const setBalanceConfig = createSetBalanceConfig({
  getAppConfig,
  findBalanceByUser,
  upsertBalanceFields,
});

router.get(
  '/managed',
  requireJwtAuth,
  createManagedUsageHandler({
    url: process.env.STARBRIDGE_USAGE_URL,
    fetch: global.fetch,
    headers: getCodeApiAuthHeaders,
  }),
);
router.post('/managed/refill', requireJwtAuth, express.json({ limit: '1kb' }), createManagedUsageHandler({
  url: process.env.STARBRIDGE_USAGE_URL, fetch: global.fetch, headers: getCodeApiAuthHeaders,
}));
router.post('/managed/consent', requireJwtAuth, express.json({ limit: '1kb' }), createManagedUsageHandler({
  url: process.env.STARBRIDGE_USAGE_URL, fetch: global.fetch, headers: getCodeApiAuthHeaders,
}));
router.get('/', requireJwtAuth, setBalanceConfig, controller);
const purchases = require('@librechat/api').createManagedPurchasesHandler({
  url: process.env.STARBRIDGE_FILES_URL,
  issuer: process.env.OPENID_ISSUER,
  serviceToken: process.env.CODECHAT_SERVICE_TOKEN,
  subject: process.env.STARBRIDGE_PAYMENT_SUBJECT,
  fetch: global.fetch,
});
router.get('/purchases', requireJwtAuth, purchases);
router.post('/purchases/create', requireJwtAuth, (req, res, next) =>
  process.env.STARBRIDGE_TOKEN_ONLY === 'true' ? res.status(410).json({code:'use_token_refill'}) : next());
router.post('/purchases/:action', requireJwtAuth, express.json({ limit: '4kb' }), purchases);
router.use('/purchases/:orderId/:action', requireJwtAuth, purchases);

module.exports = router;
