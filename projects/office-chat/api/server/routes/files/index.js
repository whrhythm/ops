const express = require('express');
const {
  createFileUsageLimiter,
  createFileLimiters,
  configMiddleware,
  requireJwtAuth,
  uaParser,
  checkBan,
} = require('~/server/middleware');
const {
  restoreTenantContextFromReq,
  createPersonalFilesHandler,
  createOfficeTasksHandler,
  getCodeApiAuthHeaders,
} = require('@librechat/api');
const { avatar: asstAvatarRouter } = require('~/server/routes/assistants/v1');
const { avatar: agentAvatarRouter } = require('~/server/routes/agents/v1');
const { createMulterInstance } = require('./multer');

const files = require('./files');
const images = require('./images');
const avatar = require('./avatar');
const speech = require('./speech');

const initialize = async () => {
  const router = express.Router();
  router.use(requireJwtAuth);
  router.use(configMiddleware);
  router.use(checkBan);
  const officeTasks = {
    url: process.env.STARBRIDGE_USAGE_URL,
    fetch: global.fetch,
    headers: getCodeApiAuthHeaders,
  };
  const { createOfficeReceiptHandler } = require('./officeReceiptQuery');
  const handleOfficeTasks = createOfficeReceiptHandler(createOfficeTasksHandler, officeTasks);
  router.get(['/office-tasks', '/office-tasks/:taskId'], handleOfficeTasks);
  router.post('/office-tasks/:taskId/cancel', handleOfficeTasks);
  router.use(
    '/personal/:operation',
    express.raw({ type: 'application/octet-stream', limit: '16mb' }),
    createPersonalFilesHandler({
      url: process.env.STARBRIDGE_FILES_URL,
      issuer: process.env.OPENID_ISSUER,
      serviceToken: process.env.CODECHAT_SERVICE_TOKEN,
      fetch: global.fetch,
      officeTasks,
      scanUrl: process.env.STARBRIDGE_SCAN_URL,
      scanToken: process.env.STARBRIDGE_SCAN_TOKEN,
      scanSubject: process.env.STARBRIDGE_SCAN_SUBJECT,
      scanEnabled: process.env.STARBRIDGE_SCAN_ENABLED === 'true',
    }),
  );
  router.use(uaParser);

  const upload = await createMulterInstance();
  router.post('/speech/stt', upload.single('audio'), restoreTenantContextFromReq);

  /* Important: speech route must be added before the upload limiters */
  router.use('/speech', speech);

  const { fileUploadIpLimiter, fileUploadUserLimiter } = createFileLimiters();
  const fileUsageLimiter = createFileUsageLimiter();

  /** Non-strict routing means `/usage/` reaches the same handler, so match the
   *  route the way Express does. An exact comparison would push a
   *  trailing-slash request onto the upload quota instead. */
  const isUsagePath = (path) => path.replace(/\/+$/, '') === '/usage';

  /** Apply rate limiters to all POST routes (excluding /speech which is handled
   *  above). `/usage` is a metadata touch, so it gets its own limiter rather
   *  than consuming upload quota, but it is never left unmetered. */
  router.use((req, res, next) => {
    if (req.method !== 'POST' || req.path.startsWith('/speech')) {
      return next();
    }
    if (isUsagePath(req.path)) {
      return fileUsageLimiter(req, res, next);
    }
    return fileUploadIpLimiter(req, res, (err) => {
      if (err) {
        return next(err);
      }
      return fileUploadUserLimiter(req, res, next);
    });
  });

  router.use('/word-pages', require('./wordPages').wordPagesRouter());
  router.post('/', upload.single('file'), restoreTenantContextFromReq);
  router.post('/images', upload.single('file'), restoreTenantContextFromReq);
  router.post('/images/avatar', upload.single('file'), restoreTenantContextFromReq);
  router.post(
    '/images/agents/:agent_id/avatar',
    upload.single('file'),
    restoreTenantContextFromReq,
  );
  router.post(
    '/images/assistants/:assistant_id/avatar',
    upload.single('file'),
    restoreTenantContextFromReq,
  );

  router.use('/', files);
  router.use('/images', images);
  router.use('/images/avatar', avatar);
  router.use('/images/agents', agentAvatarRouter);
  router.use('/images/assistants', asstAvatarRouter);
  return router;
};

module.exports = { initialize };
