const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, 'shadow-clone-progress.tsx'), 'utf8');

const cardStart = source.indexOf('const ProgressSubtaskCard = React.memo');
assert.notEqual(cardStart, -1, 'ShadowCloneProgress should define ProgressSubtaskCard');
const cardSource = source.slice(cardStart, source.indexOf('const ProgressSubtaskGrid', cardStart));

assert.match(
  cardSource,
  /data-testid="shadow-clone-recent-tool-call"/,
  'ProgressSubtaskCard should render recent tool-call summaries visibly in the card, not only in the hover tooltip',
);
assert.match(
  cardSource,
  /recentActivity\.slice\(0,\s*2\)/,
  'Visible card summaries should be bounded to avoid noisy cards',
);

console.log('ShadowCloneProgress visible tool-call summary contract passed');
