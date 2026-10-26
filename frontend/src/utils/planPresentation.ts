import type { PlanEventData, StepEventData } from '../types/event';
import type { Message, MessageContent, StepContent } from '../types/message';
import type { AnalysisOutcome } from '../types/analysisOutcome';
import { analysisOutcomeIsComplete, readAnalysisOutcome } from './analysisOutcome.ts';

export { ANALYSIS_COMPLETION_DESCRIPTION } from './analysisOutcome.ts';

export type PlanDisplayStatus = StepEventData['status'] | 'partial' | 'stopped' | 'unknown';

export const PLAN_STATUS_LABELS: Record<PlanDisplayStatus, string> = {
  completed: 'Task Completed',
  partial: 'Task Partially Completed',
  failed: 'Task Failed',
  stopped: 'Task Stopped',
  running: 'Task Running',
  pending: 'Task Pending',
  unknown: '结果待确认',
};

function outcomeStatus(outcome: AnalysisOutcome): PlanDisplayStatus {
  if (analysisOutcomeIsComplete(outcome)) return 'completed';
  if (['user_cancelled', 'request_cancelled', 'execution_interrupted', 'tool_authorization_stopped', 'model_runtime_stopped'].includes(outcome.reason_code)) return 'stopped';
  // A nominally successful outcome with missing/blocking deliverables is not complete.
  return outcome.status === 'failed' ? 'failed' : 'partial';
}

/** Use only structured outcomes for the matching step in the current user turn.
 * Session completion/done merely means execution ended, not that it succeeded.
 */
export function presentPlan(plan: PlanEventData, messages: Message[] = []) {
  const outcomes = new Map<string, AnalysisOutcome>();
  const observed = new Map<string, StepContent['status']>();
  for (const message of messages) {
    if (message.type === 'user') { outcomes.clear(); observed.clear(); }
    if (message.type === 'step') { const step = message.content as StepContent; observed.set(step.id, step.status); }
    if (message.type !== 'assistant') continue;
    const metadata = (message.content as MessageContent).metadata;
    if (typeof metadata?.step_id !== 'string' || !metadata.step_id) continue;
    const outcome = readAnalysisOutcome(metadata.analysis_outcome);
    if (outcome) outcomes.set(metadata.step_id, outcome);
  }
  const steps = plan.steps.map(step => ({
    ...step,
    displayStatus: outcomes.has(step.id) ? outcomeStatus(outcomes.get(step.id)!) : observed.get(step.id) ?? step.status,
  }));
  const completed = steps.filter(step => step.displayStatus === 'completed').length;
  const active = steps.find(step => step.displayStatus === 'running')
    ?? steps.find(step => step.displayStatus === 'pending');
  let status: PlanDisplayStatus = 'pending';
  if (active) status = active.displayStatus;
  else if (steps.length && completed === steps.length) status = 'completed';
  else if (steps.some(step => step.displayStatus === 'partial') || completed > 0) status = 'partial';
  else if (steps.some(step => step.displayStatus === 'failed')) status = 'failed';
  else if (steps.some(step => step.displayStatus === 'stopped')) status = 'stopped';
  else if (steps.some(step => step.displayStatus === 'unknown')) status = 'unknown';
  return { steps, status, label: PLAN_STATUS_LABELS[status], activeDescription: active?.description, completed, total: steps.length };
}
