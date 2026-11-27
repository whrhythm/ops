'use strict';
const { createHmac } = require('node:crypto');

// Only the platform's two fixed endpoints accept the short-lived, account-scoped credential.
function managedSearchCredentials(config, userId, origin = process.env.STARBRIDGE_SEARCH_ORIGIN) {
  if (!origin) return config;
  if (config.tavilySearchUrl !== origin + '/search' || config.tavilyExtractUrl !== origin + '/extract') {
    throw new Error('Managed search configuration incomplete');
  }
  if (!userId || !config.tavilyApiKey) throw new Error('Missing search identity');
  const mac = value => createHmac('sha256', config.tavilyApiKey).update(value).digest('hex');
  const subject = mac('account:' + userId);
  const payload = subject + '.' + Math.floor(Date.now() / 1000);
  return { ...config, tavilyApiKey: payload + '.' + mac('ticket:' + payload) };
}
module.exports = { managedSearchCredentials };
