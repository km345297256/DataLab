export interface ClarificationQuestion {
  id: string;
  question: string;
  reason: string;
  options: string[];
}

export interface AnalysisClarification {
  id: string;
  stage: 'request' | 'execution';
  source_event_seq?: number | null;
  questions: ClarificationQuestion[];
}

export interface ClarificationAnswer {
  clarificationId: string;
  message: string;
}
