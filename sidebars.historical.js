const fs = require('fs');
const path = require('path');

const docsPath = path.resolve(process.env.DOCS_PATH || 'docs');

function docIds(directory, prefix = '') {
  const ids = [];
  for (const entry of fs.readdirSync(directory, {withFileTypes: true})) {
    if (entry.name.startsWith('.')) continue;
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      ids.push(...docIds(path.join(directory, entry.name), relative));
    } else if (entry.name.endsWith('.md')) {
      ids.push(relative.slice(0, -3));
    }
  }
  return ids;
}

const ids = docIds(docsPath).sort((left, right) => {
  if (left === 'index') return -1;
  if (right === 'index') return 1;
  return left.localeCompare(right);
});

/** @type {import('@docusaurus/plugin-content-docs').SidebarsConfig} */
module.exports = {docs: ids};
