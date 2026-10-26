import type { ProgramAttemptView } from './programAttempt';
import type { ConnectorResultPanel } from '../components/ConnectorResults.vue';
import type { FileInfo } from '../api/file';
import type { MessageEventData, SpillArtifactNotice } from './event';
import type { ToolPresentation } from './toolPresentation';
import type { AnalysisJobView } from './analysisJob';
import type { ToolApprovalView } from './toolApproval';
import type { SkillCreationProgress, SkillCreationView } from './skillCreation';

export type MessageType = "user" | "assistant" | "tool" | "step" | "task-summary" | "attachments";

export interface Message {
  type: MessageType;
  content: BaseContent;
}

export interface BaseContent {
  timestamp: number;
}

export interface MessageContent extends BaseContent {
  attachments?: FileInfo[];
  content: string;
  metadata?: {
    client_message_id?: string;
    clarification_id?: string;
    clarification?: NonNullable<MessageEventData['metadata']>['clarification'];
    step_id?: string;
    analysis_outcome?: NonNullable<MessageEventData['metadata']>['analysis_outcome'];
    analysis_progress?: NonNullable<MessageEventData['metadata']>['analysis_progress'];
    skill_creation_failure?: NonNullable<MessageEventData['metadata']>['skill_creation_failure'];
    skills?: string[];
    mcp_servers?: string[];
    data_connectors?: string[];
    dataset_ids?: string[];
    safety_review?: NonNullable<MessageEventData['metadata']>['safety_review'];
  };
}

export interface ToolContent extends BaseContent {
  tool_call_id: string;
  name: string;
  function: string;
  args: any;
  content?: any;
  presentation?: ToolPresentation | null;
  spill?: SpillArtifactNotice | null;
  analysis_job?: AnalysisJobView | null;
  tool_approval?: ToolApprovalView | null;
  program_attempt?: ProgramAttemptView | null;
  execution_status?: 'succeeded' | 'failed' | 'not_started' | null;
  write_action?: 'reuse' | 'execute_saved' | 'finalize_saved' | 'inspect_saved' | null;
  connector_status?: string | null;
  connector_results?: ConnectorResultPanel | null;
  open_data_search_id?: string | null;
  open_data_update_id?: string | null;
  skill_creation?: SkillCreationView | null;
  skill_creation_progress?: SkillCreationProgress | null;
  status: "calling" | "called";
}

export interface StepContent extends BaseContent {
  id: string;
  description: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'unknown';
  tools: ToolContent[];
  started_at?: number;
  ended_at?: number;
}

export interface TaskSummaryContent extends BaseContent {
  duration_ms: number;
  has_steps: boolean;
}

export interface AttachmentsContent extends BaseContent {
  role: "user" | "assistant";
  attachments: FileInfo[];
}

export function isConsecutiveAssistant(messages: Message[], index: number): boolean {
  if (index <= 0) return false;
  const isAst = (m: Message) =>
    m.type === 'assistant' ||
    (m.type === 'attachments' && (m.content as AttachmentsContent).role === 'assistant');
  if (!isAst(messages[index])) return false;
  let previousIndex = index - 1;
  while (previousIndex >= 0 && messages[previousIndex].type === 'task-summary') previousIndex -= 1;
  return previousIndex >= 0 && isAst(messages[previousIndex]);
}
