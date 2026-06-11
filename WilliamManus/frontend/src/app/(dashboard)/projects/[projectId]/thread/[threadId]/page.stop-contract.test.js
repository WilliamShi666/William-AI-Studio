import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const testFilePath = fileURLToPath(import.meta.url);
const testDir = path.dirname(testFilePath);
const pagePath = path.join(testDir, 'page.tsx');

const readHandleStopAgentBody = () => {
  const source = readFileSync(pagePath, 'utf8');
  const match = source.match(
    /const handleStopAgent = useCallback\(async \(\) => \{([\s\S]*?)\n\s*\}, \[/,
  );

  assert.ok(match, 'expected handleStopAgent definition in page.tsx');
  return {
    source,
    body: match[1],
  };
};

test('handleStopAgent preserves shadow clone runtime after stop', () => {
  const { source, body } = readHandleStopAgentBody();

  assert.doesNotMatch(
    body,
    /resetShadowCloneRuntime\(\)/,
    'stop handler should not clear shadow clone runtime; terminal state must remain visible after stop',
  );
  assert.match(
    source,
    /useEffect\(\(\) => \{\s*resetShadowCloneRuntime\(\);[\s\S]*setPendingAssistantLoaderRunId\(null\);[\s\S]*\}, \[threadId, resetShadowCloneRuntime\]\);/,
    'shadow clone runtime should still reset on thread change',
  );
});

test('thread-change shadow clone reset is registered before useThreadData can bootstrap completed V2 runs', () => {
  const source = readFileSync(pagePath, 'utf8');
  const resetEffectIndex = source.indexOf('resetShadowCloneRuntime();');
  const useThreadDataIndex = source.indexOf('useThreadData(threadId, projectId)');

  assert.notEqual(resetEffectIndex, -1, 'expected thread-change shadow clone reset effect');
  assert.notEqual(useThreadDataIndex, -1, 'expected useThreadData call');
  assert.ok(
    resetEffectIndex < useThreadDataIndex,
    'thread-change reset effect must be registered before useThreadData effects so historical completed V2 bootstrap is not cleared after sync',
  );
});

test('handleStopAgent delegates stop submission to stopStreaming only once', () => {
  const { body } = readHandleStopAgentBody();

  assert.doesNotMatch(
    body,
    /stopAgentMutation\.mutateAsync\(/,
    'stop handler should not send a second stop request after stopStreaming already stopped the run',
  );
});

test('newly started user run remains user initiated until stream startup effect consumes it', () => {
  const source = readFileSync(pagePath, 'utf8');
  const match = source.match(
    /logThreadDebug\('✅ \[handleSubmitMessage\] Agent started successfully:'[\s\S]*?startStreaming\(newAgentRunId\);/,
  );

  assert.ok(match, 'expected successful startAgent block in handleSubmitMessage');
  assert.match(
    match[0],
    /setUserInitiatedRun\(true\)/,
    'first user request must mark the run as user-initiated so the run output panel and stream-start effect can activate',
  );
});

test('successful user-initiated run clears the startup latch immediately after streaming begins', () => {
  const source = readFileSync(pagePath, 'utf8');
  const block = source.match(
    /logThreadDebug\('✅ \[handleSubmitMessage\] Agent started successfully:'[\s\S]*?\n\s*\} catch \(err\) \{/,
  );

  assert.ok(
    block,
    'expected successful startAgent block in handleSubmitMessage',
  );
  assert.match(
    block[0],
    /startStreaming\(newAgentRunId\);\s*setUserInitiatedRun\(false\);/,
    'handleSubmitMessage must clear the userInitiatedRun latch in the same success block immediately after the direct stream starts',
  );
});



test('closing the side panel from subagent inspection keeps the selected subagent so the left chat can stay contextual', () => {
  const source = readFileSync(pagePath, 'utf8');
  const match = source.match(/const handleSidePanelClose = useCallback\(\(\) => \{([\s\S]*?)\n\s*\}, \[/);

  assert.ok(match, 'expected handleSidePanelClose definition in page.tsx');
  assert.doesNotMatch(
    match[1],
    /clearShadowCloneActiveSubtask\(\)/,
    'closing the side panel should not clear the selected subagent; only the back-to-main action should do that',
  );
});

test('subagent inspection keeps chat transcript left and tool activity panel right', () => {
  const source = readFileSync(pagePath, 'utf8');

  assert.match(
    source,
    /messages=\{effectiveThreadMessages\}[\s\S]*streamingTextContent=\{effectiveStreamingTextContent\}[\s\S]*agentName=\{effectiveThreadAgentName\}/,
    'left ThreadContent should switch to the selected subagent transcript while in subagent inspection mode',
  );
  assert.doesNotMatch(
    source,
    /isSubagentInspectionMode\s*\?\s*\(/,
    'subagent inspection should not replace the bottom chat input with a secondary status card; the chat panel itself identifies the selected subagent',
  );
});

test('selected subagent live text keeps ThreadContent in streaming render mode', () => {
  const source = readFileSync(pagePath, 'utf8');
  const statusBlock = source.match(
    /const effectiveThreadStreamHookStatus = isDeferredSubagentInspectionMode[\s\S]*?: streamHookStatus;/,
  );

  assert.ok(
    statusBlock,
    'expected selected-subagent ThreadContent streamHookStatus derivation',
  );
  assert.match(
    source,
    /const hasSelectedSubagentLiveTranscript = Boolean\([\s\S]*effectiveStreamingTextContent\.trim\(\)[\s\S]*effectiveStreamingReasoningContent\.trim\(\)[\s\S]*effectiveStreamingToolCall[\s\S]*\);/,
    'selected subagent live assistant text, reasoning, or tool state should be detected before deriving ThreadContent render status',
  );
  assert.match(
    source,
    /const shouldRenderSelectedSubagentStreamingState =[\s\S]*hasSelectedSubagentLiveTranscript[\s\S]*effectiveThreadAgentStatus === 'running'[\s\S]*effectiveThreadAgentStatus === 'connecting';/,
    'selected subagent live transcript should participate in the selected-subagent streaming render-state guard',
  );
  assert.match(
    statusBlock[0],
    /shouldRenderSelectedSubagentStreamingState/,
    'selected subagent live assistant text must keep ThreadContent renderable even if local task status lags or flips terminal',
  );
});

test('main chat panel receives Shadow Clone V2 progress through normal thread messages', () => {
  const source = readFileSync(pagePath, 'utf8');

  assert.match(
    source,
    /mergeShadowCloneMainTranscriptMessages/,
    'main view should merge Shadow Clone main transcript progress into the actual ThreadContent messages path',
  );
  assert.match(
    source,
    /const effectiveThreadMessages = isDeferredSubagentInspectionMode[\s\S]*deferredActiveShadowCloneTranscript\?\.messages \|\| \[\][\s\S]*mergeShadowCloneMainTranscriptMessages\(\s*messages,\s*deferredShadowCloneMainTranscript\?\.messages \|\| \[\]/,
    'main view ThreadContent must include mainTranscriptState progress while deferred subagent inspection keeps selected subagent transcript ownership',
  );
  assert.match(
    source,
    /<ThreadContent[\s\S]*messages=\{effectiveThreadMessages\}/,
    'effectiveThreadMessages must feed the actual ThreadContent chat panel',
  );
});


test('main chat panel dedupes Shadow Clone V2 synthetic final against canonical assistant final', () => {
  const source = readFileSync(pagePath, 'utf8');
  assert.match(
    source,
    /shadowCloneSyntheticMainFinalKey/,
    'mergeShadowCloneMainTranscriptMessages should compute a semantic key for synthetic main final output',
  );
  assert.match(
    source,
    /canonicalAssistantContentKeys/,
    'mergeShadowCloneMainTranscriptMessages should compare synthetic main final output against canonical assistant content',
  );
  assert.match(
    source,
    /shadow-clone:main:assistant-complete/,
    'dedupe should specifically handle the synthetic main assistant-complete message id',
  );
});


test('main synthetic final dedupe is timestamp scoped so prior identical assistant text stays visible for current run', () => {
  const source = readFileSync(pagePath, 'utf8');
  assert.match(
    source,
    /canonicalAssistantContentKeys\.set/,
    'canonical assistant dedupe should track timestamp per content key instead of thread-global content-only presence',
  );
  assert.match(
    source,
    /syntheticFinalTimestamp/,
    'synthetic final dedupe should compare against the synthetic final timestamp',
  );
  assert.match(
    source,
    /canonicalTimestamp >= syntheticFinalTimestamp/,
    'synthetic final should only be suppressed by same-or-later canonical assistant content',
  );
});

test('stream close directly reconciles canonical messages into chat panel state', () => {
  const source = readFileSync(pagePath, 'utf8');
  const closeMatch = source.match(/const handleStreamClose = useCallback\(\([\s\S]*?\n\s*\}, \[/);
  assert.ok(closeMatch, 'expected handleStreamClose definition in page.tsx');

  assert.match(
    source,
    /import \{[\s\S]*\bgetMessages\b[\s\S]*\} from '@\/lib\/api';/,
    'Thread page should import getMessages for direct post-terminal reconciliation',
  );
  assert.match(
    source,
    /mergeThreadMessages/,
    'Thread page should use the same merge helper as useThreadData when directly reconciling canonical messages',
  );
  assert.match(
    closeMatch[0],
    /scheduleCanonicalMessageReconciliation\(/,
    'stream close must trigger direct canonical message reconciliation, not only React Query refetch',
  );
});

test('successful run start schedules canonical reconciliation independent of stream close', () => {
  const source = readFileSync(pagePath, 'utf8');
  const block = source.match(
    /logThreadDebug\('✅ \[handleSubmitMessage\] Agent started successfully:'[\s\S]*?\n\s*\} catch \(err\) \{/,
  );

  assert.ok(
    block,
    'expected successful startAgent block in handleSubmitMessage',
  );
  assert.match(
    source,
    /const scheduleCanonicalMessageReconciliation = useCallback\(/,
    'Thread page should define a shared canonical reconciliation scheduler',
  );
  assert.match(
    block[0],
    /startStreaming\(newAgentRunId\);[\s\S]*scheduleCanonicalMessageReconciliation\(\s*`run-start:\$\{newAgentRunId\}`/,
    'a started run must poll canonical messages so visible chat recovers even when SSE close/replay is missed',
  );
  assert.match(
    block[0],
    /\[1000, 2000, 4000, 8000, 15000, 30000, 60000, 120000\]/,
    'run-start reconciliation should continue long enough to cover delayed backend persistence',
  );
});

test('successful run start keeps the pending assistant loader visible until output arrives', () => {
  const source = readFileSync(pagePath, 'utf8');
  const block = source.match(
    /logThreadDebug\('✅ \[handleSubmitMessage\] Agent started successfully:'[\s\S]*?\n\s*\} catch \(err\) \{/,
  );

  assert.ok(
    block,
    'expected successful startAgent block in handleSubmitMessage',
  );
  assert.match(
    source,
    /const \[pendingAssistantLoaderRunId, setPendingAssistantLoaderRunId\] = useState<string \| null>\(null\);/,
    'Thread page should track a page-level pending assistant loader latch',
  );
  assert.match(
    block[0],
    /setPendingAssistantLoaderRunId\(newAgentRunId\);[\s\S]*startStreaming\(newAgentRunId\);/,
    'a started run should show the original AgentLoader immediately, before SSE/canonical output arrives',
  );
  assert.match(
    source,
    /hasPendingAssistantOutput[\s\S]*hasAssistantMessageForRun/,
    'pending loader should clear based on the same renderable assistant run guard used by terminal reconciliation',
  );
  assert.match(
    source,
    /showPendingAssistantLoader=\{showPendingAssistantLoader\}/,
    'Thread page should pass the pending loader latch into the user-visible ThreadContent',
  );
  assert.match(
    source,
    /const showPendingAssistantLoader =[\s\S]*isSending[\s\S]*pendingAssistantLoaderRunId/,
    'pending loader should appear immediately while the user submission/start request is still in flight',
  );
});

test('selected subagent chat content uses deferred inspection state consistently with the selected panel', () => {
  const source = readFileSync(pagePath, 'utf8');

  assert.match(
    source,
    /const effectiveThreadMessages = isDeferredSubagentInspectionMode[\s\S]*deferredActiveShadowCloneTranscript\?\.messages \|\| \[\]/,
    'ThreadContent messages should switch to the deferred selected subagent transcript whenever the selected subagent panel is active',
  );
  assert.match(
    source,
    /const effectiveStreamingTextContent = isDeferredSubagentInspectionMode[\s\S]*deferredActiveShadowCloneTranscript\?\.streamingTextContent \|\| ''/,
    'ThreadContent streaming text should use the same deferred selected subagent transcript as the visible selected panel',
  );
  assert.match(
    source,
    /const effectiveStreamingReasoningContent = isDeferredSubagentInspectionMode[\s\S]*deferredActiveShadowCloneTranscript\?\.streamingReasoningContent \|\| ''/,
    'ThreadContent reasoning stream should use the same deferred selected subagent transcript as the visible selected panel',
  );
  assert.match(
    source,
    /const effectiveStreamingToolCall = isDeferredSubagentInspectionMode[\s\S]*deferredActiveShadowCloneTranscript\?\.streamingToolCall \|\| null/,
    'ThreadContent tool stream should use the same deferred selected subagent transcript as the visible selected panel',
  );
});
