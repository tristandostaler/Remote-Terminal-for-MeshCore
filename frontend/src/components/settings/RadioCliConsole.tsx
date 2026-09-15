import { useRef, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { api } from '../../api';

interface CliEntry {
  id: number;
  command: string;
  reply: string | null;
  error: string | null;
  elapsedMs: number | null;
}

/**
 * A one-line console to the companion firmware's own CLI (the same commands
 * its serial console accepts). Needs companion protocol 14 or newer; the
 * server answers 501 otherwise and the message is shown as-is.
 */
export function RadioCliConsole({
  connected,
  defaultOpen = false,
}: {
  connected: boolean;
  defaultOpen?: boolean;
}) {
  const [command, setCommand] = useState('');
  const [busy, setBusy] = useState(false);
  const [entries, setEntries] = useState<CliEntry[]>([]);
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
      setEntries((prev) => [
        ...prev,
        { id, command: text, reply: null, error: message, elapsedMs: null },
      ]);
    } finally {
      setBusy(false);
    }
  };

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
        <p className="text-[0.75rem] text-muted-foreground">
          Runs on the radio exactly like its serial console, so commands that change radio
          parameters or reboot the node take effect immediately and RemoteTerm's own settings may
          need a refresh. Needs companion protocol 14 or newer.
        </p>
      </div>
    </details>
  );
}
