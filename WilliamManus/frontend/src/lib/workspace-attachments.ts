function extractStringCandidates(input: unknown): string[] {
  if (Array.isArray(input)) {
    return input.filter((item): item is string => typeof item === "string");
  }

  if (typeof input !== "string") {
    return [];
  }

  const raw = input.trim();
  if (!raw) {
    return [];
  }

  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      return parsed.filter((item): item is string => typeof item === "string");
    }
    if (typeof parsed === "string") {
      return [parsed];
    }
  } catch {
    // Fall through to permissive parsing for malformed inputs.
  }

  return raw.split(",");
}

function normalizeCandidate(value: string): string | null {
  let cleaned = value.trim();
  if (!cleaned) {
    return null;
  }

  cleaned = cleaned
    .replace(/^[\s"'`[\],]+/, "")
    .replace(/[\s"'`[\],]+$/, "");

  if (!cleaned) {
    return null;
  }

  if (cleaned.startsWith("/workspace/") || cleaned === "/workspace") {
    return cleaned.replace(/\/{2,}/g, "/");
  }

  if (cleaned.startsWith("workspace/") || cleaned === "workspace") {
    return `/${cleaned}`.replace(/\/{2,}/g, "/");
  }

  if (cleaned.startsWith("/")) {
    return cleaned.replace(/\/{2,}/g, "/");
  }

  const relative = cleaned.replace(/^\.\//, "");
  return `/workspace/${relative}`.replace(/\/{2,}/g, "/");
}

export function normalizeWorkspaceAttachmentPaths(input: unknown): string[] {
  const seen = new Set<string>();
  const result: string[] = [];

  for (const candidate of extractStringCandidates(input)) {
    const normalized = normalizeCandidate(candidate);
    if (!normalized || seen.has(normalized)) {
      continue;
    }
    seen.add(normalized);
    result.push(normalized);
  }

  return result;
}
