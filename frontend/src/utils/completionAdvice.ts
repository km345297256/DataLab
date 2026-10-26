import type { CompletionAdviceData } from '../types/event';

export const SKILL_CREATOR_PROMPT = '/skill-creator 将这个流程保存为可复用的技能';

/** Older saved turns put the skill action in the recommendation list. Display
 * their actual follow-ups without duplicating the separate skill action or
 * inventing a replacement question for that historical turn. */
export function analysisFollowUps(advice: CompletionAdviceData): string[] {
  return advice.recommendations.filter(question => !/\/skill-creat(?:e|or)\b/i.test(question)).slice(0, 3);
}
