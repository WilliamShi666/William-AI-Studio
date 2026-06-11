export type SandboxPathKind = 'file' | 'directory' | 'unknown';

export interface SandboxPathEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
  mod_time: string;
}

export interface SandboxPathMatch {
  entry: SandboxPathEntry | null;
  kind: SandboxPathKind;
  normalizedPath: string;
}

export function classifySandboxPathFromEntries(
  rawPath: string,
  entries: SandboxPathEntry[],
  normalizePath: (path: string) => string,
): SandboxPathMatch {
  const normalizedPath = normalizePath(rawPath);
  const matchedEntry = entries.find((entry) => normalizePath(entry.path) === normalizedPath);

  if (!matchedEntry) {
    return {
      entry: null,
      kind: 'unknown',
      normalizedPath,
    };
  }

  return {
    entry: {
      ...matchedEntry,
      path: normalizedPath,
    },
    kind: matchedEntry.is_dir ? 'directory' : 'file',
    normalizedPath,
  };
}
