import { useCallback, useMemo, useState } from 'react';
import { ChevronDown, ChevronRight, Check, X } from 'lucide-react';

import { cn } from '@/lib/utils';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { RefreshIcon, useArmedAction } from './repeaterPaneShared';
import type {
  RepeaterSettingApplyResult,
  RepeaterSettingChange,
  RepeaterSettingDefinition,
  RepeaterSettingsSchemaResponse,
  RepeaterSettingValue,
} from '../../types';

const SELECT_CLASS =
  'h-8 rounded-md border border-input bg-background px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50';

/** Where a field's shown text comes from, and whether the operator changed it. */
function draftOf(
  setting: RepeaterSettingDefinition,
  drafts: Record<string, string>,
  values: Record<string, RepeaterSettingValue>
): string {
  const draft = drafts[setting.key];
  if (draft !== undefined) return draft;
  return values[setting.key]?.value ?? '';
}

function isDirty(
  setting: RepeaterSettingDefinition,
  drafts: Record<string, string>,
  values: Record<string, RepeaterSettingValue>
): boolean {
  const draft = drafts[setting.key];
  if (draft === undefined) return false;
  return draft.trim() !== (values[setting.key]?.value ?? '').trim();
}

/** A setting the firmware answered "??" to cannot be written, so its field is locked. */
function isUnsupported(value: RepeaterSettingValue | undefined): boolean {
  return value?.status === 'unsupported';
}

function statusNote(value: RepeaterSettingValue | undefined): string | null {
  if (!value) return null;
  switch (value.status) {
    case 'unsupported':
      return 'Not supported by this firmware';
    case 'error':
      return value.raw ? `Firmware error: ${value.raw}` : 'The firmware refused to read this';
    case 'no_reply':
      return 'No reply — out of range, or this session is not an admin';
    default:
      return null;
  }
}

const RADIO_PLACEHOLDERS = ['MHz', 'kHz', 'SF', 'CR'];
const RADIO_STEPS = ['0.001', '0.1', '1', '1'];
const RADIO_WIDTHS = ['w-24', 'w-20', 'w-16', 'w-16'];

/** The four parts of a `freq,bw,sf,cr` tuple, padded so every box renders. */
function radioParts(value: string): string[] {
  const parts = value.split(',').map((part) => part.trim());
  return [0, 1, 2, 3].map((index) => parts[index] ?? '');
}

function SettingField({
  setting,
  value,
  draft,
  dirty,
  result,
  disabled,
  onChange,
}: {
  setting: RepeaterSettingDefinition;
  value: RepeaterSettingValue | undefined;
  draft: string;
  dirty: boolean;
  result: RepeaterSettingApplyResult | undefined;
  disabled: boolean;
  onChange: (next: string) => void;
}) {
  const unsupported = isUnsupported(value);
  const locked = disabled || unsupported || !setting.writable;
  const note = statusNote(value);
  const inputId = `repeater-setting-${setting.key}`;

  const control = (() => {
    if (setting.value_type === 'bool') {
      return (
        <select
          id={inputId}
          className={cn(SELECT_CLASS, 'w-28')}
          value={draft === '' ? '' : draft}
          disabled={locked}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">—</option>
          <option value="on">On</option>
          <option value="off">Off</option>
        </select>
      );
    }

    if (setting.value_type === 'enum') {
      // A firmware that answers with a value we don't know about still has to
      // be shown, so the current reading is offered alongside the known ones.
      const options =
        draft !== '' && !setting.options.includes(draft)
          ? [...setting.options, draft]
          : setting.options;
      return (
        <select
          id={inputId}
          className={cn(SELECT_CLASS, 'w-36')}
          value={draft}
          disabled={locked}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">—</option>
          {options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      );
    }

    if (setting.value_type === 'radio') {
      const parts = radioParts(draft);
      return (
        <div className="flex flex-wrap items-center gap-1">
          {parts.map((part, index) => (
            <Input
              key={index}
              id={index === 0 ? inputId : undefined}
              type="number"
              inputMode="decimal"
              step={RADIO_STEPS[index]}
              className={cn('h-8 text-sm', RADIO_WIDTHS[index])}
              placeholder={RADIO_PLACEHOLDERS[index]}
              aria-label={`${setting.label} ${RADIO_PLACEHOLDERS[index]}`}
              value={part}
              disabled={locked}
              onChange={(e) => {
                const next = [...parts];
                next[index] = e.target.value;
                onChange(next.join(','));
              }}
            />
          ))}
        </div>
      );
    }

    const numeric = setting.value_type === 'int' || setting.value_type === 'float';
    return (
      <Input
        id={inputId}
        type={setting.sensitive ? 'password' : numeric ? 'number' : 'text'}
        autoComplete={setting.sensitive ? 'new-password' : 'off'}
        inputMode={setting.value_type === 'int' ? 'numeric' : undefined}
        step={setting.step ?? undefined}
        min={numeric && setting.minimum != null ? setting.minimum : undefined}
        max={numeric && setting.maximum != null ? setting.maximum : undefined}
        maxLength={setting.max_length ?? undefined}
        className="h-8 w-44 text-sm"
        placeholder={setting.readable ? '' : 'write-only'}
        value={draft}
        disabled={locked}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  })();

  return (
    <div
      className={cn(
        'flex flex-wrap items-start justify-between gap-x-3 gap-y-1 border-b border-border/50 px-3 py-2 last:border-b-0',
        dirty && 'bg-primary/5'
      )}
    >
      <div className="min-w-0 flex-[1_1_14rem]">
        <label
          htmlFor={inputId}
          className={cn('text-sm', unsupported ? 'text-muted-foreground' : 'font-medium')}
        >
          {setting.label}
          {setting.unit && <span className="ml-1 text-muted-foreground">({setting.unit})</span>}
        </label>
        <p className="text-[0.6875rem] text-muted-foreground">
          {setting.help} <span className="font-mono opacity-70">{setting.command_hint}</span>
        </p>
        {setting.note && <p className="text-[0.6875rem] text-warning italic">{setting.note}</p>}
        {note && <p className="text-[0.6875rem] text-muted-foreground italic">{note}</p>}
        {result && (
          <p
            className={cn(
              'flex items-center gap-1 text-[0.6875rem]',
              result.status === 'ok' ? 'text-success' : 'text-destructive'
            )}
          >
            {result.status === 'ok' ? (
              <Check className="h-3 w-3" aria-hidden="true" />
            ) : (
              <X className="h-3 w-3" aria-hidden="true" />
            )}
            {result.reply ?? result.status}
          </p>
        )}
      </div>
      <div className="ml-auto flex items-center gap-2">
        {dirty && (
          <span
            className="h-1.5 w-1.5 rounded-full bg-primary"
            title="Changed — not yet applied"
            aria-label="Changed"
          />
        )}
        {control}
      </div>
    </div>
  );
}

export function SettingsEditorPane({
  schema,
  values,
  loading,
  error,
  cliResponsive,
  disabled,
  onFetch,
  onApply,
}: {
  schema: RepeaterSettingsSchemaResponse | null;
  values: Record<string, RepeaterSettingValue>;
  loading: boolean;
  error: string | null;
  cliResponsive: boolean | null;
  disabled?: boolean;
  onFetch: (filter?: { keys?: string[]; group?: string }) => Promise<void>;
  onApply: (changes: RepeaterSettingChange[]) => Promise<RepeaterSettingApplyResult[]>;
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  // Which group's "Read" is in flight, and whether an apply is -- `loading`
  // alone covers both, and the footer must not say "Applying" during a read.
  const [readingGroup, setReadingGroup] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({});
  const [results, setResults] = useState<Record<string, RepeaterSettingApplyResult>>({});

  const busy = loading || !!disabled;

  const settingsByGroup = useMemo(() => {
    const grouped = new Map<string, RepeaterSettingDefinition[]>();
    for (const setting of schema?.settings ?? []) {
      const list = grouped.get(setting.group);
      if (list) list.push(setting);
      else grouped.set(setting.group, [setting]);
    }
    return grouped;
  }, [schema]);

  const dirtyChanges = useMemo((): RepeaterSettingChange[] => {
    const changes: RepeaterSettingChange[] = [];
    for (const setting of schema?.settings ?? []) {
      if (!isDirty(setting, drafts, values)) continue;
      const draft = (drafts[setting.key] ?? '').trim();
      if (draft === '') continue; // Clearing a field is not a write.
      changes.push({ key: setting.key, value: draft });
    }
    return changes;
  }, [schema, drafts, values]);

  const handleChange = useCallback((key: string, next: string) => {
    setDrafts((prev) => ({ ...prev, [key]: next }));
    // A field being edited again drops its previous verdict.
    setResults((prev) => {
      if (!(key in prev)) return prev;
      const next2 = { ...prev };
      delete next2[key];
      return next2;
    });
  }, []);

  const applyChanges = useCallback(async () => {
    if (dirtyChanges.length === 0) return;
    setApplying(true);
    let applied: RepeaterSettingApplyResult[];
    try {
      applied = await onApply(dirtyChanges);
    } finally {
      setApplying(false);
    }
    const byKey: Record<string, RepeaterSettingApplyResult> = {};
    for (const result of applied) byKey[result.key] = result;
    setResults(byKey);
    // Keep only the fields that did not take, so the pane still shows what is
    // outstanding and one more click retries exactly those.
    setDrafts((prev) => {
      const next = { ...prev };
      for (const result of applied) {
        if (result.status === 'ok') delete next[result.key];
      }
      return next;
    });
  }, [dirtyChanges, onApply]);

  const [confirmApply, handleApply] = useArmedAction(applyChanges);

  const revert = useCallback(() => {
    setDrafts({});
    setResults({});
  }, []);

  const groups = schema?.groups ?? [];

  return (
    <div className="col-span-full overflow-hidden rounded-lg border border-border">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-muted/50 px-3 py-2">
        <div className="min-w-0">
          <h3 className="text-sm font-medium">Settings Editor</h3>
          <p className="text-[0.6875rem] text-muted-foreground">
            Reads and writes the repeater's own configuration. Admin login required — the firmware
            answers no CLI command for a guest.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            className="h-7 px-2 text-[0.6875rem] sm:h-8 sm:px-3 sm:text-xs"
            disabled={busy || !schema}
            onClick={() => void onFetch()}
            title="Read every setting. One CLI round trip per setting, so this takes a while."
          >
            <RefreshIcon className={cn('mr-1 h-3 w-3', loading && 'animate-spin')} />
            Read All
          </Button>
        </div>
      </div>

      {error && (
        <div className="border-b border-border bg-destructive/5 px-3 py-1.5 text-xs text-destructive">
          {error}
        </div>
      )}
      {cliResponsive === false && (
        <div className="border-b border-border bg-destructive/5 px-3 py-1.5 text-xs text-destructive">
          Nothing answered. The firmware routes CLI commands for admins only, so log in with the
          admin password before editing.
        </div>
      )}

      {!schema ? (
        <p className="p-3 text-sm text-muted-foreground italic">Loading the settings catalog...</p>
      ) : (
        <div>
          {groups.map((group) => {
            const groupSettings = settingsByGroup.get(group.key) ?? [];
            if (groupSettings.length === 0) return null;
            // A group opens itself once it has something to show -- after its
            // own read, or after "Load All" read the lot -- unless the operator
            // has since toggled it by hand.
            const hasValues = groupSettings.some((setting) => setting.key in values);
            const open = openGroups[group.key] ?? hasValues;
            const dirtyCount = groupSettings.filter((setting) =>
              isDirty(setting, drafts, values)
            ).length;

            return (
              <div key={group.key} className="border-b border-border last:border-b-0">
                <div className="flex items-center justify-between gap-2 px-3 py-2">
                  <button
                    type="button"
                    className="flex min-w-0 flex-1 items-center gap-1.5 text-left transition-colors hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    aria-expanded={open}
                    onClick={() => setOpenGroups((prev) => ({ ...prev, [group.key]: !open }))}
                  >
                    {open ? (
                      <ChevronDown className="h-3.5 w-3.5 flex-shrink-0" aria-hidden="true" />
                    ) : (
                      <ChevronRight className="h-3.5 w-3.5 flex-shrink-0" aria-hidden="true" />
                    )}
                    <span className="truncate text-sm font-medium">{group.label}</span>
                    {dirtyCount > 0 && (
                      <span className="rounded bg-primary/15 px-1.5 py-0.5 text-[0.625rem] text-primary">
                        {dirtyCount} changed
                      </span>
                    )}
                    <span className="hidden truncate text-[0.6875rem] text-muted-foreground sm:inline">
                      {group.description}
                    </span>
                  </button>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-7 px-2 text-[0.6875rem]"
                    disabled={busy}
                    onClick={() => {
                      setOpenGroups((prev) => ({ ...prev, [group.key]: true }));
                      setReadingGroup(group.key);
                      void onFetch({ group: group.key }).finally(() =>
                        setReadingGroup((current) => (current === group.key ? null : current))
                      );
                    }}
                    title={`Read the ${group.label} settings from the repeater`}
                  >
                    {readingGroup === group.key && loading ? 'Reading...' : 'Read'}
                  </Button>
                </div>
                {open && (
                  <div className="border-t border-border/50">
                    {groupSettings.map((setting) => (
                      <SettingField
                        key={setting.key}
                        setting={setting}
                        value={values[setting.key]}
                        draft={draftOf(setting, drafts, values)}
                        dirty={isDirty(setting, drafts, values)}
                        result={results[setting.key]}
                        disabled={busy}
                        onChange={(next) => handleChange(setting.key, next)}
                      />
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border bg-muted/30 px-3 py-2">
        <p className="text-[0.6875rem] text-muted-foreground">
          {dirtyChanges.length === 0
            ? 'No pending changes. Read a group, edit a field, then apply.'
            : `${dirtyChanges.length} pending change${dirtyChanges.length === 1 ? '' : 's'}; each is one command to the repeater.`}
        </p>
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            className="h-7 px-2 text-[0.6875rem]"
            disabled={busy || dirtyChanges.length === 0}
            onClick={revert}
          >
            Revert
          </Button>
          <Button
            variant={confirmApply ? 'destructive' : 'default'}
            size="sm"
            className="h-7 px-3 text-[0.6875rem]"
            disabled={busy || dirtyChanges.length === 0}
            onClick={handleApply}
            title="Writes each changed setting to the repeater"
          >
            {applying
              ? 'Applying...'
              : confirmApply
                ? `Confirm ${dirtyChanges.length} change${dirtyChanges.length === 1 ? '' : 's'}`
                : dirtyChanges.length === 0
                  ? 'Apply changes'
                  : `Apply ${dirtyChanges.length} change${dirtyChanges.length === 1 ? '' : 's'}`}
          </Button>
        </div>
      </div>
    </div>
  );
}
