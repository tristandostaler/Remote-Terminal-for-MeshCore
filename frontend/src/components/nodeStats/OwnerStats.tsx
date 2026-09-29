/**
 * Who runs this node: what the node itself reported (`owner.info`), the ways to
 * reach the owner spotted in it, and the operator's own notes.
 *
 * Unlike every other section this one writes: the notes and the "contacted"
 * mark are edited in place. It still never *fetches* -- the shell's one request
 * carries the owner record, and a save hands the updated record back up through
 * `onChange` so the page stays one snapshot.
 *
 * `OwnerHints` and `OwnerNotesEditor` are shared with the owner outreach view.
 */

import { useEffect, useState } from 'react';
import { AtSign, Globe, Mail, Radio } from 'lucide-react';

import { api } from '../../api';
import { Button } from '../ui/button';
import { toast } from '../ui/sonner';
import type { ContactOwnerHint, ContactOwnerInfo } from '../../types';
import { formatDateTime, StatRow, StatSection } from './nodeStatsShared';

const NOTES_MAX_LENGTH = 2000;

const HINT_ICONS: Record<ContactOwnerHint['kind'], typeof Mail> = {
  callsign: Radio,
  email: Mail,
  handle: AtSign,
  url: Globe,
};

const HINT_SOURCES: Record<ContactOwnerHint['source'], string> = {
  name: 'node name',
  owner_info: 'owner info',
  notes: 'your notes',
};

const ATTEMPT_LABELS: Record<string, string> = {
  ok: 'answered',
  no_reply: 'no reply',
  login_failed: 'login refused',
  error: 'failed',
};

function hintHref(hint: ContactOwnerHint): string | null {
  if (hint.kind === 'email') return `mailto:${hint.value}`;
  if (hint.kind === 'url') return hint.value;
  if (hint.kind === 'callsign') return `https://www.qrz.com/db/${encodeURIComponent(hint.value)}`;
  return null;
}

export function OwnerHints({ hints }: { hints: ContactOwnerHint[] }) {
  if (hints.length === 0) return null;
  return (
    <ul className="flex flex-wrap gap-1.5" aria-label="Ways to reach the owner">
      {hints.map((hint) => {
        const Icon = HINT_ICONS[hint.kind];
        const href = hintHref(hint);
        const body = (
          <>
            <Icon className="h-3 w-3 shrink-0" aria-hidden="true" />
            <span className="break-all">{hint.value}</span>
          </>
        );
        const className =
          'inline-flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-xs';
        return (
          <li
            key={`${hint.kind}:${hint.value}`}
            title={`${hint.kind}, from ${HINT_SOURCES[hint.source]}`}
          >
            {href ? (
              <a
                href={href}
                target="_blank"
                rel="noreferrer"
                className={`${className} hover:bg-muted`}
              >
                {body}
              </a>
            ) : (
              <span className={className}>{body}</span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** Edits the notes and the contacted mark; reports every saved record upward. */
export function OwnerNotesEditor({
  owner,
  onChange,
  compact = false,
}: {
  owner: ContactOwnerInfo;
  onChange: (next: ContactOwnerInfo) => void;
  compact?: boolean;
}) {
  const [draft, setDraft] = useState(owner.notes);
  const [saving, setSaving] = useState(false);

  // A save elsewhere (or a refresh) replaces the record; follow it unless the
  // operator is mid-edit, which would silently discard what they typed.
  const dirty = draft !== owner.notes;
  useEffect(() => {
    if (!dirty) setDraft(owner.notes);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [owner.notes]);

  const save = async (update: { notes?: string; notified?: boolean }) => {
    setSaving(true);
    try {
      const next = await api.updateContactOwner(owner.public_key, update);
      onChange(next);
      if (update.notes !== undefined) setDraft(next.notes);
    } catch (err) {
      toast.error('Could not save owner notes', {
        description: err instanceof Error ? err.message : undefined,
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-2">
      <textarea
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        maxLength={NOTES_MAX_LENGTH}
        rows={compact ? 2 : 3}
        placeholder="Callsign, forum handle, where you met them, what you told them…"
        aria-label="Notes about the owner"
        className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" disabled={!dirty || saving} onClick={() => void save({ notes: draft })}>
          Save notes
        </Button>
        {dirty && (
          <Button size="sm" variant="ghost" disabled={saving} onClick={() => setDraft(owner.notes)}>
            Discard
          </Button>
        )}
        <Button
          size="sm"
          variant="outline"
          disabled={saving}
          aria-pressed={owner.notified_at !== null}
          onClick={() => void save({ notified: owner.notified_at === null })}
          title={
            owner.notified_at === null
              ? 'Record that you told the owner, so the outreach list gives them time'
              : 'Clear the contacted mark'
          }
        >
          {owner.notified_at === null ? 'Mark contacted' : 'Clear contacted mark'}
        </Button>
        {owner.notified_at !== null && (
          <span className="text-xs text-muted-foreground">
            Contacted {formatDateTime(owner.notified_at)}
          </span>
        )}
        {owner.notes_updated_at !== null && !compact && (
          <span className="ml-auto text-xs text-muted-foreground">
            Notes saved {formatDateTime(owner.notes_updated_at)}
          </span>
        )}
      </div>
    </div>
  );
}

export function OwnerStats({
  owner,
  isServer,
  onChange,
}: {
  owner: ContactOwnerInfo;
  /** Repeaters and rooms report owner.info; other nodes only have notes. */
  isServer: boolean;
  onChange: (next: ContactOwnerInfo) => void;
}) {
  const attempt = owner.attempt_status
    ? (ATTEMPT_LABELS[owner.attempt_status] ?? owner.attempt_status)
    : null;

  return (
    <StatSection
      title="Owner"
      description={
        isServer
          ? 'What the node reports as its owner.info, refreshed in the background (Settings › Radio), plus your own notes. Your notes are never overwritten by a refresh.'
          : 'Only repeaters and room servers report an owner. Keep what you know about who runs this node here.'
      }
    >
      <div className="space-y-3">
        {isServer && (
          <div className="rounded-md border border-border p-3">
            {owner.firmware_owner_info ? (
              <p className="whitespace-pre-wrap break-words text-sm">
                {owner.firmware_owner_info.replace(/\|/g, '\n')}
              </p>
            ) : (
              <p className="text-sm text-muted-foreground">
                {owner.fetched_at !== null
                  ? 'The node answered with no owner info set.'
                  : 'Not fetched yet. Open the repeater and load Owner Info, or wait for the background refresh.'}
              </p>
            )}
            <div className="mt-2 space-y-0.5">
              {owner.firmware_version && (
                <StatRow label="Firmware" value={owner.firmware_version} />
              )}
              {owner.fetched_at !== null && (
                <StatRow label="Last answered" value={formatDateTime(owner.fetched_at)} />
              )}
              {owner.attempted_at !== null && owner.attempt_status !== 'ok' && attempt && (
                <StatRow
                  label="Last attempt"
                  value={`${formatDateTime(owner.attempted_at)} — ${attempt}`}
                />
              )}
            </div>
          </div>
        )}
        <OwnerHints hints={owner.hints} />
        <OwnerNotesEditor owner={owner} onChange={onChange} />
      </div>
    </StatSection>
  );
}
