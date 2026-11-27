const express = require('express');
const { createManagedCredentialGuard } = require('@librechat/api');
const { updateUserKey, deleteUserKey, getUserKeyExpiry } = require('~/models');
const { configMiddleware, requireJwtAuth } = require('~/server/middleware');

const router = express.Router();
const managedCredentialGuard = createManagedCredentialGuard({
  enabled: Boolean(process.env.CODECHAT_PROVISION_URL),
});

router.put('/', requireJwtAuth, managedCredentialGuard, configMiddleware, async (req, res) => {
  if (req.body == null || typeof req.body !== 'object') {
    return res.status(400).send({ error: 'Invalid request body.' });
  }
  const { name, value, expiresAt } = req.body;
  await updateUserKey({
    userId: req.user.id,
    name,
    value,
    expiresAt,
    writeFormat: req.config?.credentials?.writeFormat,
  });
  res.status(201).send();
});

router.delete('/:name', requireJwtAuth, managedCredentialGuard, async (req, res) => {
  const { name } = req.params;
  await deleteUserKey({ userId: req.user.id, name });
  res.status(204).send();
});

router.delete('/', requireJwtAuth, managedCredentialGuard, async (req, res) => {
  const { all } = req.query;

  if (all !== 'true') {
    return res.status(400).send({ error: 'Specify either all=true to delete.' });
  }

  await deleteUserKey({ userId: req.user.id, all: true });

  res.status(204).send();
});

router.get('/', requireJwtAuth, async (req, res) => {
  const { name } = req.query;
  const response = await getUserKeyExpiry({ userId: req.user.id, name });
  res.status(200).send(response);
});

module.exports = router;
