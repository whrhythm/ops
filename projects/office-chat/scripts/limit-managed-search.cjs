// Bound the model-facing preview at the formatter, before the agent adds tool history.
// Leave search results and source URLs in the web-search artifact intact.
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(path.dirname(require.resolve('@librechat/agents')), '..');
for (const [format, ext] of [['cjs', 'cjs'], ['esm', 'mjs']]) {
  const file = path.join(root, format, 'tools', 'search', 'format.' + ext);
  const original = fs.readFileSync(file, 'utf8');
  const flag = 'process.env.STARBRIDGE_SEARCH_ORIGIN';
  if (original.includes(flag)) continue;
  const edits = [
    ['outputLines.push(`Summary: ${source.snippet}`)',
      'outputLines.push(`Summary: ${' + flag + ' && source.snippet.length > 320 ? Array.from(source.snippet).slice(0,320).join("") + "…" : source.snippet}`)'],
    ['trimHighlightsToBudget(results, resolveMaxLLMOutputChars(maxOutputChars))',
      'trimHighlightsToBudget(results, Math.min(resolveMaxLLMOutputChars(maxOutputChars), ' + flag + ' ? 1400 : Infinity))'],
  ];
  let updated = original;
  for (const [before, after] of edits) {
    if (updated.split(before).length !== 2) throw new Error('Unrecognized search formatter in ' + file);
    updated = updated.replace(before, () => after);
  }
  fs.writeFileSync(file, updated);
}
