import type { AnalysisClarification } from '../types/clarification';

/** Malformed or future metadata must never become an actionable confirmation. */
export function readAnalysisClarification(value: unknown): AnalysisClarification | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const request = value as Record<string, unknown>;
  if (typeof request.id !== 'string' || !request.id.trim()
    || !['request', 'execution'].includes(String(request.stage))
    || !Array.isArray(request.questions) || !request.questions.length || request.questions.length > 3) return undefined;
  const ids = new Set<string>();
  for (const question of request.questions) {
    if (!question || typeof question !== 'object' || typeof question.id !== 'string' || !question.id.trim()
      || ids.has(question.id) || typeof question.question !== 'string' || !question.question.trim()
      || typeof question.reason !== 'string' || !Array.isArray(question.options)
      || !question.options.every((option: unknown) => typeof option === 'string' && option.trim())) return undefined;
    ids.add(question.id);
  }
  return value as AnalysisClarification;
}

/** Include each question with its explicit answer; never supply a default. */
export function clarificationAnswerText(request: AnalysisClarification, answers: Record<string, string>): string | undefined {
  if (!request.questions.every(question => typeof answers[question.id] === 'string' && answers[question.id].trim())) return undefined;
  return request.questions.map((question, index) =>
    `${index + 1}. ${question.question}\n我的确认：${answers[question.id].trim()}`,
  ).join('\n\n');
}
