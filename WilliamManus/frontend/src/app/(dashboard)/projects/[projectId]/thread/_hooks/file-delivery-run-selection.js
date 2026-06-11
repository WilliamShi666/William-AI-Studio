export function getAgentRunSortTimestamp(run) {
  const candidateTimestamps = [run?.completed_at, run?.started_at];

  for (const candidateTimestamp of candidateTimestamps) {
    if (!candidateTimestamp) continue;
    const parsedTimestamp = Date.parse(candidateTimestamp);
    if (Number.isFinite(parsedTimestamp)) {
      return parsedTimestamp;
    }
  }

  return 0;
}

function hasBrowseSource(run) {
  return Boolean(run?.file_delivery_source?.browseSandboxId);
}

function hasThreadWorkspaceSource(run) {
  return run?.file_delivery_source?.identitySource === 'thread_workspace_artifacts';
}

function latestRun(runs) {
  return [...runs].sort(
    (leftRun, rightRun) =>
      getAgentRunSortTimestamp(rightRun) - getAgentRunSortTimestamp(leftRun),
  )[0] ?? null;
}

function readRunMetadata(run) {
  const metadata = run?.metadata;
  if (metadata && typeof metadata === 'object' && !Array.isArray(metadata)) {
    return metadata;
  }
  if (typeof metadata !== 'string' || !metadata.trim()) {
    return {};
  }
  try {
    const parsed = JSON.parse(metadata);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed
      : {};
  } catch {
    return {};
  }
}

function isShadowCloneV2Run(run) {
  const metadata = readRunMetadata(run);
  const mode = String(metadata.shadow_clone_mode || '').trim().toLowerCase();
  const runtime = String(metadata.shadow_clone_runtime || '').trim().toLowerCase();
  return runtime === 'v2' || mode === 'v2' || mode === 'on' || mode === 'auto';
}

function filterRunsForThread(runs, threadId) {
  if (!threadId) {
    return runs;
  }
  return runs.filter((run) => !run?.thread_id || run.thread_id === threadId);
}

export function selectPreferredAgentRun(runs, preferredRunId, threadId = null) {
  const scopedRuns = filterRunsForThread(runs ?? [], threadId);
  if (!scopedRuns || scopedRuns.length === 0) {
    return null;
  }

  if (preferredRunId) {
    const matchingRun = scopedRuns.find((run) => run.id === preferredRunId);
    if (matchingRun) {
      return matchingRun;
    }
  }

  const runningRuns = scopedRuns.filter((run) => run.status === 'running');
  if (runningRuns.length > 0) {
    return latestRun(runningRuns);
  }

  return latestRun(scopedRuns);
}

export function selectPreferredFileDeliveryRun(runs, preferredRunId, threadId = null) {
  const scopedRuns = filterRunsForThread(runs ?? [], threadId);
  if (!scopedRuns || scopedRuns.length === 0) {
    return null;
  }

  if (preferredRunId) {
    const matchingRun = scopedRuns.find((run) => run.id === preferredRunId);
    if (matchingRun?.file_delivery_source?.browseSandboxId) {
      return matchingRun;
    }
  }

  const threadWorkspaceRuns = scopedRuns.filter(
    (run) => hasBrowseSource(run) && hasThreadWorkspaceSource(run),
  );
  if (threadWorkspaceRuns.length > 0) {
    return latestRun(threadWorkspaceRuns);
  }

  const runningWithSource = scopedRuns.filter(
    (run) => run.status === 'running' && hasBrowseSource(run),
  );
  if (runningWithSource.length > 0) {
    return latestRun(runningWithSource);
  }

  const terminalWithSource = scopedRuns.filter(hasBrowseSource);
  if (terminalWithSource.length > 0) {
    return latestRun(terminalWithSource);
  }

  return selectPreferredAgentRun(scopedRuns, preferredRunId);
}

export function selectShadowCloneBootstrapRun(runs, preferredRunId, threadId = null) {
  const scopedRuns = filterRunsForThread(runs ?? [], threadId).filter(isShadowCloneV2Run);
  if (scopedRuns.length === 0) {
    return null;
  }

  if (preferredRunId) {
    const matchingRun = scopedRuns.find((run) => run.id === preferredRunId);
    if (matchingRun) {
      return matchingRun;
    }
  }

  const runningRuns = scopedRuns.filter((run) => run.status === 'running');
  if (runningRuns.length > 0) {
    return latestRun(runningRuns);
  }

  return latestRun(
    scopedRuns.filter((run) =>
      run.status === 'completed' ||
      run.status === 'stopped' ||
      run.status === 'error'
    ),
  );
}
