import { useRef, useState } from 'react';
import { ChevronDown, Info } from 'lucide-react';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { ApiError, api } from '../../api';

/** Shown when the server told us on a previous connection-scoped attempt, so we have no message of our own. */
const DEFAULT_UNSUPPORTED_MESSAGE =
  "This radio's firmware does not accept CLI commands over the companion link. The MeshCore CLI " +
  'belongs to repeater and room-server firmware; a companion (chat client) radio has no CLI to ' +
  'run. Use the repeater CLI to reach those nodes over the mesh.';

interface CliEntry {
  id: number;
  command: string;
  reply: string | null;
  error: string | null;
  elapsedMs: number | null;
}

/**
 * A one-line console to the radio firmware's own CLI.
 *
 * Most radios cannot do this: the MeshCore CLI belongs to repeater and
 * room-server firmware, and a companion (chat client) radio has no CLI to run,
 * so it refuses the command as unknown. The server reports that as 501 and
 * remembers it for the connection (`config.cli_unsupported`), so instead of
 * failing identically on every attempt the console says so once and stops
 * offering the input.
 */
export function RadioCliConsole({
  connected,
  unsupported = false,
  defaultOpen = false,
}: {
  connected: boolean;
  /** Server already established this firmware has no CLI (radio config). */
  unsupported?: boolean;
  defaultOpen?: boolean;
}) {
  const [command, setCommand] = useState('');
  const [busy, setBusy] = useState(false);
  const [entries, setEntries] = useState<CliEntry[]>([]);
  // Latched from a 501 so the input stops being offered without waiting for
  // the radio config to be refetched.
  const [refused, setRefused] = useState<string | null>(null);
  const nextId = useRef(1);
  const history = useRef<string[]>([]);
  const historyIndex = useRef<number | null>(null);

  const run = async () => {
    const text = command.trim();
    if (!text || busy) return;
    const id = nextId.current++;
    history.current.push(text);
    historyIndex.current = null;
    setBusy(true);
    setCommand('');
    try {
      const response = await api.runRadioCli(text);
      setEntries((prev) => [
        ...prev,
        { id, command: text, reply: response.reply, error: null, elapsedMs: response.elapsed_ms },
      ]);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Command failed';
      if (err instanceof ApiError && err.status === 501) {
        // Not a failed command: this firmware has no CLI. Say it once, in
        // place of the input, rather than as one more error line.
        setRefused(message);
      } else {
        setEntries((prev) => [
          ...prev,
          { id, command: text, reply: null, error: message, elapsedMs: null },
        ]);
      }
    } finally {
      setBusy(false);
    }
  };

  const noCli = unsupported || refused !== null;

  const recall = (direction: -1 | 1) => {
    const items = history.current;
    if (items.length === 0) return;
    const current = historyIndex.current ?? items.length;
    const next = Math.min(items.length, Math.max(0, current + direction));
    historyIndex.current = next === items.length ? null : next;
    setCommand(next === items.length ? '' : items[next]);
  };

  return (
    <details className="group" open={defaultOpen || undefined}>
      <summary className="text-sm font-medium text-foreground cursor-pointer select-none flex items-center gap-1">
        <ChevronDown className="h-3 w-3 transition-transform group-open:rotate-0 -rotate-90" />
        Radio CLI
      </summary>
      <div className="mt-2 space-y-2 rounded-md border border-input bg-muted/20 p-3">
        {entries.length > 0 && (
          <div
            className="max-h-64 overflow-y-auto rounded border border-input bg-background p-2 font-mono text-xs space-y-2"
            data-testid="radio-cli-output"
          >
            {entries.map((entry) => (
              <div key={entry.id}>
                <div className="text-muted-foreground">
                  &gt; {entry.command}
                  {entry.elapsedMs != null && (
                    <span className="ml-2 text-[0.65rem]">{entry.elapsedMs} ms</span>
                  )}
                </div>
                {entry.reply != null && (
                  <pre className="whitespace-pre-wrap break-words">
                    {entry.reply || '(empty reply)'}
                  </pre>
                )}
                {entry.error && (
                  <pre className="whitespace-pre-wrap break-words text-destructive">
                    {entry.error}
                  </pre>
                )}
              </div>
            ))}
          </div>
        )}
        {noCli ? (
          <div className="flex items-start gap-2 rounded border border-input bg-background p-2">
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            <p className="text-xs text-muted-foreground" data-testid="radio-cli-unsupported">
              {refused ?? DEFAULT_UNSUPPORTED_MESSAGE}
            </p>
          </div>
        ) : (
          <form
            className="flex items-center gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              void run();
            }}
          >
            <Input
              value={command}
              onChange={(event) => setCommand(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'ArrowUp') {
                  event.preventDefault();
                  recall(-1);
                } else if (event.key === 'ArrowDown') {
                  event.preventDefault();
                  recall(1);
                }
              }}
              placeholder={connected ? 'e.g. ver, get freq, set af 1' : 'Radio is not connected'}
              disabled={!connected || busy}
              maxLength={160}
              className="font-mono text-xs"
              aria-label="Radio CLI command"
              autoComplete="off"
              spellCheck={false}
            />
            <Button
              type="submit"
              variant="outline"
              size="sm"
              disabled={!connected || busy || !command.trim()}
            >
              {busy ? 'Sending...' : 'Send'}
            </Button>
            {entries.length > 0 && (
              <Button type="button" variant="ghost" size="sm" onClick={() => setEntries([])}>
                Clear
              </Button>
            )}
          </form>
        )}
        {!noCli && (
          <p className="text-[0.75rem] text-muted-foreground">
            Runs on the radio exactly like its own console, so commands that change radio parameters
            or reboot the node take effect immediately and RemoteTerm&apos;s own settings may need a
            refresh. Only repeater and room-server firmware has a CLI.
          </p>
        )}
      </div>
    </details>
  );
}
