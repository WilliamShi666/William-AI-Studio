const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, 'ThreadContent.tsx'), 'utf8');

assert.match(
  source,
  /data-testid="thread-user-message-group"/,
  'ThreadContent should expose stable user message group test markers for chat-panel E2E',
);

assert.match(
  source,
  /data-testid="thread-assistant-message-group"/,
  'ThreadContent should expose stable assistant message group test markers for chat-panel E2E',
);

assert.match(
  source,
  /showPendingAssistantLoader\?: boolean;/,
  'ThreadContent should accept an explicit pending assistant loader prop for the post-submit blank window',
);

assert.match(
  source,
  /showPendingAssistantLoader\s*\|\|[\s\S]*agentStatus === 'running'/,
  'ThreadContent should render AgentLoader when a page-level pending assistant output latch is active',
);

assert.match(
  source,
  /msgKey\.includes\('streaming'\)[\s\S]*parsedContent\.content \|\| message\.content/,
  'ThreadContent must render synthetic streaming assistant messages from raw streamingTextContent when they are plain text, not only JSON {content} payloads',
);

console.log('ThreadContent test marker contract passed');
