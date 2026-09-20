import { describe, it, expect } from 'vitest';
import { findActiveMentionQuery, filterMentionCandidates } from '../utils/mentionAutocomplete';

describe('findActiveMentionQuery', () => {
  it('detects a bare "@" at the cursor', () => {
    expect(findActiveMentionQuery('hey @', 5)).toEqual({ start: 4, query: '' });
  });

  it('detects a partially typed name after "@"', () => {
    expect(findActiveMentionQuery('hey @ali', 8)).toEqual({ start: 4, query: 'ali' });
  });

  it('only matches when "@" starts the text or follows whitespace/bracket', () => {
    expect(findActiveMentionQuery('name@node.local', 16)).toBeNull();
    expect(findActiveMentionQuery('@Alice', 6)).toEqual({ start: 0, query: 'Alice' });
    expect(findActiveMentionQuery('(@Alice', 7)).toEqual({ start: 1, query: 'Alice' });
  });

  it('stops the query at a space -- the mention is already finished', () => {
    // Cursor sits after "hey @Alice " (past the trailing space): the mention
    // is done, so there is nothing left to suggest.
    expect(findActiveMentionQuery('hey @Alice there', 11)).toBeNull();
  });

  it('is anchored to the cursor, not the whole string', () => {
    // Cursor sits right after "@ali", before the rest of the line was typed.
    expect(findActiveMentionQuery('hey @alice, are you there', 8)).toEqual({
      start: 4,
      query: 'ali',
    });
  });

  it('returns null with no "@" before the cursor', () => {
    expect(findActiveMentionQuery('hello world', 5)).toBeNull();
  });
});

describe('filterMentionCandidates', () => {
  const names = ['Alice', 'Alicia', 'Bob', 'Charlie', 'alice2'];

  it('matches case-insensitively by substring', () => {
    expect(filterMentionCandidates(names, 'ali')).toEqual(['Alice', 'Alicia', 'alice2']);
  });

  it('returns everything (capped) for an empty query', () => {
    expect(filterMentionCandidates(names, '')).toEqual(names);
  });

  it('caps the result to the given limit', () => {
    expect(filterMentionCandidates(names, 'a', 2)).toHaveLength(2);
  });

  it('returns an empty list when nothing matches', () => {
    expect(filterMentionCandidates(names, 'zzz')).toEqual([]);
  });
});
