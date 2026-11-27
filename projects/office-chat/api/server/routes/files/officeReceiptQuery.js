/** Forward only the bounded list of hashes; the existing handler authenticates the owner and selects the upstream. */
function createOfficeReceiptHandler(createHandler, deps) {
  const standard = createHandler(deps);
  return (req, res) => {
    const raw = req.query.call_key;
    if (raw === undefined) return standard(req, res);
    const keys = Array.isArray(raw) ? raw : [raw];
    if (req.method !== 'GET' || req.params.taskId || keys.length > 16 ||
        keys.some(key => typeof key !== 'string' || !/^[a-f0-9]{64}$/.test(key))) {
      return res.status(400).json({ code: 'office_task_invalid' });
    }
    return createHandler({
      ...deps,
      fetch: (target, options) => {
        const url = new URL(target);
        for (const key of keys) url.searchParams.append('call_key', key);
        return deps.fetch(url, options);
      },
    })(req, res);
  };
}
module.exports = { createOfficeReceiptHandler };
