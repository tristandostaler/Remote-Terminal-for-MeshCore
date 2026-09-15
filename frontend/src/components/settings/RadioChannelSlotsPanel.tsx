import { useCallback, useState } from 'react';
import { ChevronDown, RefreshCw } from 'lucide-react';
import { Button } from '../ui/button';
import { api } from '../../api';
import type { RadioChannelSlot, RadioChannelSlotsResponse } from '../../types';

function slotStatus(slot: RadioChannelSlot): { label: string; tone: string } {
  if (slot.empty) return { label: 'Empty', tone: 'text-muted-foreground' };
  if (slot.resident) return { label: 'Resident', tone: 'text-status-connected' };
  if (slot.send_cache) return { label: 'Send cache', tone: 'text-foreground' };
  if (slot.known_name) return { label: 'On radio', tone: 'text-foreground' };
  return { label: 'Not joined', tone: 'text-warning' };
}

/**
 * The radio's channel slots as the radio reports them, not as RemoteTerm
 * believes them to be. Reads every slot on demand (one command per slot, so a
 * few seconds over TCP) and never on a timer.
 */
export function RadioChannelSlotsPanel({
  connected,
  defaultOpen = false,
}: {
  connected: boolean;
  defaultOpen?: boolean;
}) {
  const [data, setData] = useState<RadioChannelSlotsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showEmpty, setShowEmpty] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.getRadioChannelSlots());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to read the radio');
    } finally {
      setLoading(false);
    }
  }, []);

  const occupied = data?.slots.filter((s) => !s.empty) ?? [];
  const emptyCount = data ? data.slots.length - occupied.length : 0;
  const rows = showEmpty ? (data?.slots ?? []) : occupied;

  return (
    <details
      className="group"
      open={defaultOpen || undefined}
      onToggle={(event) => {
        if ((event.currentTarget as HTMLDetailsElement).open && data === null && !loading) {
          void refresh();
        }
      }}
    >
      <summary className="text-sm font-medium text-foreground cursor-pointer select-none flex items-center gap-1">
        <ChevronDown className="h-3 w-3 transition-transform group-open:rotate-0 -rotate-90" />
        Channels on Radio
      </summary>
      <div className="mt-2 space-y-2 rounded-md border border-input bg-muted/20 p-3">
        <div className="flex items-center justify-between gap-2">
          <p className="text-xs text-muted-foreground">
            {data
              ? `${occupied.length} of ${data.max_channels} slots in use` +
                (data.resident_enabled ? '' : ' (resident channels off)')
              : 'Read straight from the radio, one slot at a time.'}
          </p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => void refresh()}
            disabled={loading || !connected}
            aria-label="Refresh channel slots"
          >
            <RefreshCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
            <span className="ml-1">{loading ? 'Reading...' : 'Refresh'}</span>
          </Button>
        </div>

        {!connected && <p className="text-xs text-muted-foreground">Radio is not connected.</p>}
        {error && <p className="text-xs text-destructive">{error}</p>}

        {data && rows.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-muted-foreground">
                  <th className="py-1 pr-2 font-medium">Slot</th>
                  <th className="py-1 pr-2 font-medium">Name</th>
                  <th className="py-1 pr-2 font-medium">Key</th>
                  <th className="py-1 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((slot) => {
                  const status = slotStatus(slot);
                  return (
                    <tr key={slot.slot} className="border-t border-input/60">
                      <td className="py-1 pr-2 font-mono tabular-nums">{slot.slot}</td>
                      <td className="py-1 pr-2">
                        {slot.name ?? <span className="text-muted-foreground">—</span>}
                        {slot.name && slot.known_name && slot.known_name !== slot.name && (
                          <span className="text-muted-foreground">
                            {' '}
                            (joined as {slot.known_name})
                          </span>
                        )}
                      </td>
                      <td className="py-1 pr-2 font-mono" title={slot.key ?? undefined}>
                        {slot.key ? `${slot.key.slice(0, 8)}…` : ''}
                      </td>
                      <td className={`py-1 ${status.tone}`}>{status.label}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {data && emptyCount > 0 && (
          <button
            type="button"
            className="text-xs text-muted-foreground underline-offset-2 hover:underline"
            onClick={() => setShowEmpty((v) => !v)}
          >
            {showEmpty ? 'Hide' : 'Show'} {emptyCount} empty slot{emptyCount === 1 ? '' : 's'}
          </button>
        )}

        <p className="text-[0.75rem] text-muted-foreground">
          Resident slots are pinned by RemoteTerm so the radio can decrypt and queue their messages
          itself. "Not joined" means the radio holds a channel RemoteTerm does not list; it is
          cleared at the next full sync.
        </p>
      </div>
    </details>
  );
}
