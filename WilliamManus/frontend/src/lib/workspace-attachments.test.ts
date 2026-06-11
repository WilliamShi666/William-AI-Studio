import test from "node:test";
import assert from "node:assert/strict";

import { normalizeWorkspaceAttachmentPaths } from "./workspace-attachments.ts";

test("normalizes a JSON array string of workspace paths", () => {
  const input = '["/workspace/courseware/index.html", "/workspace/courseware/quadratics"]';

  assert.deepEqual(normalizeWorkspaceAttachmentPaths(input), [
    "/workspace/courseware/index.html",
    "/workspace/courseware/quadratics",
  ]);
});

test("normalizes a single malformed quoted path string", () => {
  const input = '/workspace/courseware/index.html",';

  assert.deepEqual(normalizeWorkspaceAttachmentPaths(input), [
    "/workspace/courseware/index.html",
  ]);
});

test("normalizes arrays with mixed absolute/relative paths and whitespace", () => {
  const input = ["courseware/index.html", " /workspace/courseware/quadratics "];

  assert.deepEqual(normalizeWorkspaceAttachmentPaths(input), [
    "/workspace/courseware/index.html",
    "/workspace/courseware/quadratics",
  ]);
});

test("deduplicates while preserving order and ignores empty/non-string values", () => {
  const input = [
    "courseware/index.html",
    " ",
    "/workspace/courseware/quadratics",
    "/workspace/courseware/quadratics",
    null,
    42,
    "/workspace/courseware/index.html",
  ];

  assert.deepEqual(normalizeWorkspaceAttachmentPaths(input), [
    "/workspace/courseware/index.html",
    "/workspace/courseware/quadratics",
  ]);
});
