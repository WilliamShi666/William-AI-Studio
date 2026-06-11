export const FINAL_ASSISTANT_STREAM_STATUSES = new Set([
  'complete',
  'completed',
  'final',
  'done',
]);

export const isFinalAssistantStreamStatus = (streamStatus) =>
  Boolean(streamStatus && FINAL_ASSISTANT_STREAM_STATUSES.has(streamStatus));
