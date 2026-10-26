/** Owner-safe skill creation receipt. Storage paths and credentials stay on the server. */
export interface SkillCreationProgress {
  stage: string;
  status: 'running' | 'repairing' | 'passed' | 'failed' | 'cancelled';
  attempt: number;
  code?: string | null;
}

export interface SkillCreationView {
  id: string;
  name: string;
  description: string;
  capabilities: string[];
  triggers: string[];
  applicability: string[];
  resources: string[];
  validation: {
    package: 'passed';
    script_syntax: 'passed' | 'not_applicable';
    workflow_execution: 'not_run';
    synthetic_execution?: 'passed' | 'not_run' | 'not_applicable' | 'unsupported';
    synthetic_case_count?: number;
    synthetic_passed_count?: number;
  };
  installed: boolean;
  installed_skill_id: string | null;
}
