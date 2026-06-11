const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const threadContentPath = path.resolve(__dirname, "ThreadContent.tsx");
const threadContentSource = fs.readFileSync(threadContentPath, "utf8");

test("ThreadContent imports workspace attachment normalizer", () => {
  assert.match(
    threadContentSource,
    /from\s+["']@\/lib\/workspace-attachments["']/,
  );
});

test("ThreadContent normalizes ask and complete attachments", () => {
  assert.match(
    threadContentSource,
    /normalizeWorkspaceAttachmentPaths\s*\([\s\S]{0,200}?ask[\s\S]{0,200}?\)/i,
  );
  assert.match(
    threadContentSource,
    /normalizeWorkspaceAttachmentPaths\s*\([\s\S]{0,200}?complete[\s\S]{0,200}?\)/i,
  );
});

test("ThreadContent renders Shadow Clone V2 file artifacts from assistant message metadata", () => {
  assert.match(
    threadContentSource,
    /shadow_clone_file_artifacts/,
    "Shadow Clone V2 file artifacts must be read from assistant message metadata",
  );
  assert.match(
    threadContentSource,
    /messageFileArtifactsById/,
    "artifact cards must be associated with the owning assistant message, not only the whole run",
  );
  assert.match(
    threadContentSource,
    /TaskFilesSummary[\s\S]{0,600}?shadowCloneFileArtifacts/,
    "Shadow Clone V2 artifact cards should use the shared TaskFilesSummary card UI",
  );
});
