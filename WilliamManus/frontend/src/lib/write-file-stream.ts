export interface NormalizedWriteFileArgs {
  file_path?: string;
  file_contents?: string;
  file_contents_delta?: string;
  delta_index?: number;
}

const WRITE_FILE_PATH_KEYS = ['file_path', 'path', 'target_file', 'file'] as const;
const WRITE_FILE_CONTENT_KEYS = ['file_contents', 'content'] as const;
const WRITE_FILE_DELTA_KEYS = ['file_contents_delta', 'content_delta'] as const;
const WRITE_FILE_INDEX_KEYS = ['delta_index'] as const;

const decodePartialJsonString = (rawValue: string): string => {
  const trailingBackslashes = rawValue.match(/\\+$/);
  let safeValue = rawValue;
  if (trailingBackslashes && trailingBackslashes[0].length % 2 === 1) {
    safeValue = rawValue.slice(0, -1);
  }

  try {
    return JSON.parse(`"${safeValue}"`);
  } catch {
    return safeValue
      .replace(/\\n/g, '\n')
      .replace(/\\t/g, '\t')
      .replace(/\\r/g, '\r')
      .replace(/\\"/g, '"')
      .replace(/\\\\/g, '\\');
  }
};

const looksLikeJsonStringTerminator = (tail: string): boolean => {
  if (tail === '') return true;
  return /^\s*(?:}|,\s*}|,\s*"(?:[A-Za-z0-9_]+)"\s*:|,\s*$)/.test(tail);
};

const readLenientJsonString = (rawText: string, startIdx: number): string => {
  const parts: string[] = [];
  let idx = startIdx;

  while (idx < rawText.length) {
    const current = rawText[idx];
    if (current === '\\') {
      if (idx + 1 < rawText.length) {
        parts.push(current, rawText[idx + 1]);
        idx += 2;
        continue;
      }
      parts.push(current);
      break;
    }

    if (current === '"') {
      const tail = rawText.slice(idx + 1);
      if (looksLikeJsonStringTerminator(tail)) {
        return parts.join('');
      }
    }

    parts.push(current);
    idx += 1;
  }

  return parts.join('');
};

const extractLenientJsonStringField = (
  rawArguments: string,
  fieldNames: readonly string[],
): string | undefined => {
  const fieldGroup = fieldNames.map((fieldName) => fieldName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  const match = rawArguments.match(new RegExp(`"(?:${fieldGroup})"\\s*:\\s*"`, 'm'));
  if (!match || match.index === undefined) return undefined;

  return decodePartialJsonString(
    readLenientJsonString(rawArguments, match.index + match[0].length),
  );
};

const extractNumericField = (
  rawArguments: string,
  fieldNames: readonly string[],
): number | undefined => {
  const fieldGroup = fieldNames.map((fieldName) => fieldName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  const match = rawArguments.match(
    new RegExp(`"(?:${fieldGroup})"\\s*:\\s*(-?\\d+)`, 'm'),
  );
  if (!match?.[1]) return undefined;

  const parsed = Number.parseInt(match[1], 10);
  return Number.isFinite(parsed) ? parsed : undefined;
};

const getStringField = (
  payload: Record<string, unknown>,
  fieldNames: readonly string[],
): string | undefined => {
  for (const fieldName of fieldNames) {
    if (fieldName in payload && typeof payload[fieldName] === 'string') {
      return payload[fieldName] as string;
    }
  }

  return undefined;
};

const getNumericField = (
  payload: Record<string, unknown>,
  fieldNames: readonly string[],
): number | undefined => {
  for (const fieldName of fieldNames) {
    const value = payload[fieldName];
    if (typeof value === 'number' && Number.isFinite(value)) {
      return value;
    }
    if (typeof value === 'string' && value.trim()) {
      const parsed = Number.parseInt(value, 10);
      if (Number.isFinite(parsed)) {
        return parsed;
      }
    }
  }

  return undefined;
};

const normalizeWriteFileObject = (
  payload: Record<string, unknown>,
): NormalizedWriteFileArgs | null => {
  const normalized: NormalizedWriteFileArgs = {};

  const filePath = getStringField(payload, WRITE_FILE_PATH_KEYS);
  if (typeof filePath === 'string' && filePath.trim()) {
    normalized.file_path = filePath;
  }

  const fileContents = getStringField(payload, WRITE_FILE_CONTENT_KEYS);
  if (typeof fileContents === 'string') {
    normalized.file_contents = fileContents;
  }

  const fileContentsDelta = getStringField(payload, WRITE_FILE_DELTA_KEYS);
  if (typeof fileContentsDelta === 'string') {
    normalized.file_contents_delta = fileContentsDelta;
  }

  const deltaIndex = getNumericField(payload, WRITE_FILE_INDEX_KEYS);
  if (deltaIndex !== undefined) {
    normalized.delta_index = deltaIndex;
  }

  return Object.keys(normalized).length ? normalized : null;
};

const normalizeWriteFileString = (
  rawArguments: string,
): NormalizedWriteFileArgs | null => {
  const normalized: NormalizedWriteFileArgs = {};

  const filePath = extractLenientJsonStringField(rawArguments, WRITE_FILE_PATH_KEYS);
  if (typeof filePath === 'string' && filePath.trim()) {
    normalized.file_path = filePath;
  }

  const fileContents = extractLenientJsonStringField(
    rawArguments,
    WRITE_FILE_CONTENT_KEYS,
  );
  if (typeof fileContents === 'string') {
    normalized.file_contents = fileContents;
  }

  const fileContentsDelta = extractLenientJsonStringField(
    rawArguments,
    WRITE_FILE_DELTA_KEYS,
  );
  if (typeof fileContentsDelta === 'string') {
    normalized.file_contents_delta = fileContentsDelta;
  }

  const deltaIndex = extractNumericField(rawArguments, WRITE_FILE_INDEX_KEYS);
  if (deltaIndex !== undefined) {
    normalized.delta_index = deltaIndex;
  }

  return Object.keys(normalized).length ? normalized : null;
};

export const normalizeWriteFileArgs = (
  rawArguments: unknown,
): NormalizedWriteFileArgs | null => {
  if (!rawArguments) return null;

  if (typeof rawArguments === 'string') {
    const trimmed = rawArguments.trim();
    if (!trimmed) return null;

    if (trimmed.startsWith('{')) {
      try {
        const parsed = JSON.parse(trimmed);
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
          return normalizeWriteFileObject(parsed as Record<string, unknown>);
        }
      } catch {
        return normalizeWriteFileString(trimmed);
      }
    }

    return normalizeWriteFileString(trimmed);
  }

  if (typeof rawArguments === 'object' && !Array.isArray(rawArguments)) {
    return normalizeWriteFileObject(rawArguments as Record<string, unknown>);
  }

  return null;
};

const mergeStreamText = (previous: string, incoming: string): string => {
  if (!incoming) return previous || '';
  if (!previous) return incoming;
  if (incoming.startsWith(previous)) return incoming;
  if (previous.startsWith(incoming)) return previous;

  const overlap = Math.min(previous.length, incoming.length, 256);
  for (let size = overlap; size > 0; size -= 1) {
    if (previous.endsWith(incoming.slice(0, size))) {
      return previous + incoming.slice(size);
    }
  }

  return previous + incoming;
};

export const mergeNormalizedWriteFileArgs = (
  previousRawArguments: unknown,
  incomingRawArguments: unknown,
): NormalizedWriteFileArgs | null => {
  const previousArgs = normalizeWriteFileArgs(previousRawArguments);
  const incomingArgs = normalizeWriteFileArgs(incomingRawArguments);

  if (!previousArgs) return incomingArgs;
  if (!incomingArgs) return previousArgs;

  const previousPath = previousArgs.file_path;
  const incomingPath = incomingArgs.file_path;
  if (
    typeof previousPath === 'string' &&
    previousPath &&
    typeof incomingPath === 'string' &&
    incomingPath &&
    previousPath !== incomingPath
  ) {
    return incomingArgs;
  }

  const merged: NormalizedWriteFileArgs = {};
  const mergedPath = incomingPath || previousPath;
  if (mergedPath) {
    merged.file_path = mergedPath;
  }

  const previousContents = previousArgs.file_contents || '';
  const incomingContents = incomingArgs.file_contents || '';
  const incomingDelta = incomingArgs.file_contents_delta || '';
  const previousDeltaIndex = previousArgs.delta_index;
  const incomingDeltaIndex = incomingArgs.delta_index;

  let mergedContents = previousContents;
  if (incomingContents) {
    mergedContents =
      !previousContents || incomingContents.length >= previousContents.length
        ? incomingContents
        : previousContents;
  }

  const isDuplicateDelta =
    incomingDelta.length > 0 &&
    typeof incomingDeltaIndex === 'number' &&
    incomingDeltaIndex === previousDeltaIndex;
  if (incomingDelta && !isDuplicateDelta) {
    mergedContents = mergeStreamText(mergedContents, incomingDelta);
  }

  if (mergedContents || incomingContents !== undefined || previousContents !== undefined) {
    merged.file_contents = mergedContents;
  }

  if (incomingDelta) {
    merged.file_contents_delta = incomingDelta;
  } else if (incomingArgs.file_contents_delta === '') {
    merged.file_contents_delta = '';
  }

  if (incomingDeltaIndex !== undefined) {
    merged.delta_index = incomingDeltaIndex;
  } else if (previousDeltaIndex !== undefined) {
    merged.delta_index = previousDeltaIndex;
  }

  return Object.keys(merged).length ? merged : null;
};

export const stringifyNormalizedWriteFileArgs = (
  normalizedArgs: NormalizedWriteFileArgs | null,
): string | null => {
  if (!normalizedArgs || !Object.keys(normalizedArgs).length) {
    return null;
  }

  return JSON.stringify(normalizedArgs);
};
