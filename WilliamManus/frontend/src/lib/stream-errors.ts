const normalizeStreamMessage = (value: string): string =>
  value.toLowerCase().replace(/\s+/g, ' ').trim();

const TERMINAL_STREAM_STATUSES = new Set([
  'idle',
  'completed',
  'stopped',
  'failed',
  'error',
  'agent_not_running',
]);

export const isBenignAgentNotRunningError = (message: string): boolean => {
  const normalized = normalizeStreamMessage(message);
  if (!normalized) return false;

  if (normalized.includes('not found in active runs')) {
    return true;
  }

  if (normalized.includes('agent run is not running')) {
    return true;
  }

  return /agent run .+ is not running/.test(normalized);
};

export const isLikelyStreamConnectionError = (message: string): boolean => {
  const normalized = normalizeStreamMessage(message);
  if (!normalized) return false;

  return (
    normalized.includes('stream connection error') ||
    normalized.includes('eventsource') ||
    normalized.includes('failed to fetch')
  );
};

export const isBenignPostTerminalStreamError = (
  message: string,
  status: string | null | undefined,
): boolean => {
  if (!isLikelyStreamConnectionError(message)) {
    return false;
  }

  if (!status) {
    return false;
  }

  return TERMINAL_STREAM_STATUSES.has(status);
};
