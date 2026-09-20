/**
 * Detects an in-progress "@name" the user is typing in the composer, so
 * `MessageInput` can show autocomplete suggestions and later replace it with
 * the wire mention form (`@[Name] `, see `messageContainsMention` in
 * `messageParser.ts` and `splitReplyMention` in `meshcoreOpenPayloads.ts`).
 */

export interface ActiveMentionQuery {
  /** Index of the triggering `@` within the text. */
  start: number;
  /** What has been typed after `@` so far (may be empty right after typing `@`). */
  query: string;
}

// An `@` starts a mention only at the start of the text or after whitespace/an
// opening bracket -- not mid-word (an email-like "name@node" should not pop a
// menu). The query itself stops at the next space or bracket character.
const ACTIVE_MENTION_PATTERN = /(?:^|[\s([])@([^\s@[\]]*)$/;

/** Find the `@query` immediately before `cursor`, or null when there is none. */
export function findActiveMentionQuery(text: string, cursor: number): ActiveMentionQuery | null {
  const upToCursor = text.slice(0, cursor);
  const match = ACTIVE_MENTION_PATTERN.exec(upToCursor);
  if (!match) return null;
  const query = match[1];
  return { start: cursor - query.length - 1, query };
}

/** Names matching `query.query`, case-insensitively, capped for a compact popup. */
export function filterMentionCandidates(
  candidates: readonly string[],
  query: string,
  limit = 6
): string[] {
  const needle = query.toLowerCase();
  return candidates.filter((name) => name.toLowerCase().includes(needle)).slice(0, limit);
}
