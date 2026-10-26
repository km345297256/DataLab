import type { Message, StepContent, ToolContent } from '../types/message';
import type { SkillCreationProgress, SkillCreationView } from '../types/skillCreation';

const creationStages: Record<string, string> = {
  source_analysis: '分析依据检查', method_requirements: '方法提炼', workflow: '工作流设计',
  implementation: '脚本生成', implementation_revision: '实现修订', method_review: '方法完整性审查',
  synthetic_validation_plan: '合成验证设计', synthetic_plan_review: '合成预期审查',
  synthetic_reference_implementation: '独立参考计算准备', synthetic_reference_review: '独立参考方法审查',
  synthetic_reference_execution: '独立参考计算核验', synthetic_build_repair: '程序构建修复', synthetic_script_repair: '脚本验证与修复',
  synthetic_execution: '合成用例执行', package_validation: '技能包检查', package_storage: '技能包保存',
};

export function skillCreationProgressText(progress?: SkillCreationProgress | null): string {
  if (!progress || !creationStages[progress.stage]) return '';
  const states = { running: '进行中', repairing: '修正中', passed: '已完成', failed: '未完成', cancelled: '已取消',
    response_received: '审查结果已返回', accepted: '审查通过', revision_required: '需要修订' };
  const state = progress.attempt === 0 && progress.status === 'passed' && progress.stage !== 'package_storage'
    ? '沿用已检查版本' : states[progress.status];
  return state ? `${creationStages[progress.stage]} · ${state}` : '';
}

/** Result cards sit outside collapsible execution details, including historical steps. */
export function messageSkillCreations(message: Message): SkillCreationView[] {
  const tools = message.type === 'tool' ? [message.content as ToolContent]
    : message.type === 'step' ? (message.content as StepContent).tools : [];
  const drafts = new Map<string, SkillCreationView>();
  for (const tool of tools) if (tool.skill_creation) drafts.set(tool.skill_creation.id, tool.skill_creation);
  return [...drafts.values()];
}

export function skillPackageFilename(name: string): string {
  return `${name.replace(/[\\/\u0000-\u001f\u007f<>:"|?*]/g, '-').replace(/^\.+/, '').slice(0, 120) || 'skill'}.zip`;
}
