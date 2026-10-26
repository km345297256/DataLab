import type { ToolContent } from '../types/message';
import { mergeAnalysisToolEvent } from './analysisJob.ts';
import { recoveredProgramAttempts } from './programRecovery.ts';

export interface DisplayToolItem {
  key: string;
  tool: ToolContent;
  panelTool: ToolContent;
  count: number;
  label?: string;
  summary?: string;
  revisions: ToolContent[];
  activities: ToolContent[];
  recoveredByCallId?: string;
}

const isMutation = (tool: ToolContent): boolean => tool.name === 'file'
  && ['file_write', 'file_str_replace', 'file_apply_patch'].includes(tool.function)
  && typeof tool.args?.file === 'string' && !!tool.args.file;

export const toolOperationState = (tool: ToolContent): string => {
  if (tool.status !== 'called') return '进行中';
  if (tool.execution_status === 'not_started') return tool.write_action === 'reuse' ? '沿用版本' : '未执行';
  if (tool.execution_status === 'failed') return '失败';
  if (tool.execution_status === 'succeeded') return '已完成';
  const result = tool.content?.result ?? tool.content;
  if (result?.success === false) return '失败';
  if (result?.success === true) return '已完成';
  // A terminal event alone does not attest successful execution.
  return '已结束';
};

/** Describe the requested operation, without inferring creation or correctness. */
export const toolOperationLabel = (tool: ToolContent): string => {
  if (tool.function === 'program_run') return '运行记录';
  if (tool.function === 'file_write') return tool.args?.append === true ? '追加' : '写入';
  if (['file_str_replace', 'file_apply_patch'].includes(tool.function)) return '局部修正';
  return '操作';
};

function savedOperationsSummary(revisions: ToolContent[]): string {
  const completed = new Map<string, number>();
  const states = new Map<string, number>();
  for (const revision of revisions) {
    const state = toolOperationState(revision);
    if (state === '已完成') {
      const kind = toolOperationLabel(revision);
      completed.set(kind, (completed.get(kind) || 0) + 1);
    } else {
      states.set(state, (states.get(state) || 0) + 1);
    }
  }
  const counts = ['写入', '局部修正', '追加']
    .filter(kind => completed.has(kind)).map(kind => `${kind} ${completed.get(kind)} 次`);
  const parts = [counts.length ? `保存完成：${counts.join('、')}` : '0 次保存完成'];
  for (const state of ['沿用版本', '未执行', '失败', '进行中', '已结束']) {
    const count = states.get(state);
    if (count) parts.push(`${count} 次${state === '已结束' ? '结果未确认' : state}`);
  }
  return parts.join(' · ');
}

/** Display-only aggregation within one step. No event history is rewritten. */
export function buildToolTimeline(events: ToolContent[]): DisplayToolItem[] {
  const calls = new Map<string, ToolContent>();
  for (const event of events) {
    const previous = calls.get(event.tool_call_id);
    // Status snapshots are not separate operations. Keep a result when the
    // terminal snapshot only updates job metadata without another result.
    calls.set(event.tool_call_id, previous ? mergeAnalysisToolEvent(previous, event) : { ...event });
  }
  const items: DisplayToolItem[] = [];
  const recovered = recoveredProgramAttempts([...calls.values()]);
  const groups = new Map<string, DisplayToolItem>();
  for (const tool of calls.values()) {
    // Approval and declarative output cards must remain visible individually.
    const groupable = isMutation(tool) && !tool.tool_approval && !tool.presentation;
    const target = groupable ? tool.args.file as string : '';
    const group = target ? groups.get(target) : undefined;
    // Place matching program invocations between edits in the expanded view.
    // This is chronology, not a claim that an edit fixed a failure or that a
    // later successful run validates every earlier version.
    if (tool.name === 'shell' && tool.function === 'program_run'
      && typeof tool.args?.script_path === 'string') {
      groups.get(tool.args.script_path)?.activities.push(tool);
    }
    if (group) {
      group.revisions.push(tool);
      group.activities.push(tool);
      group.count = group.revisions.length;
      group.tool = tool;
      group.panelTool = tool;
    } else {
      const item = { key: tool.tool_call_id, tool, panelTool: tool, count: 1,
        revisions: groupable ? [tool] : [], activities: groupable ? [tool] : [],
        recoveredByCallId: recovered.get(tool.tool_call_id) };
      items.push(item);
      if (target) groups.set(target, item);
    }
  }
  for (const group of groups.values()) {
    if (group.count > 1) {
      // The latest operation remains the detail-panel target, not the group's
      // title: an initial write followed by a patch is not two replacements.
      group.label = '文件保存记录';
      group.summary = savedOperationsSummary(group.revisions);
    }
  }
  return items;
}
