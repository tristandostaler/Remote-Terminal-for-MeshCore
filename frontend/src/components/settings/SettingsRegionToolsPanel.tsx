import { useState } from 'react';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { toast } from '../ui/sonner';
import { api } from '../../api';
import type { GuessRegionsResponse, ImportRegionsResponse } from '../../types';

/**
 * Two ways to fill Known Regions without asking a repeater: guess names against
 * stored region-scoped packets, and import a list published on a website.
 *
 * A packet's region code is a one-way hash, so "guess" tests candidate names
 * (every 2/3-letter code, province/state pairs like onqc, plus anything imported
 * here) and reports the ones that explain several stored packets.
 */
export function RegionToolsPanel({ onAddRegions }: { onAddRegions: (names: string[]) => void }) {
  const [guessing, setGuessing] = useState(false);
  const [guess, setGuess] = useState<GuessRegionsResponse | null>(null);
  const [url, setUrl] = useState('');
  const [importing, setImporting] = useState(false);
  const [imported, setImported] = useState<ImportRegionsResponse | null>(null);

  const runGuess = async (candidates: string[] = []) => {
    setGuessing(true);
    try {
      const data = await api.guessRegions(candidates);
      setGuess(data);
      if (data.tested_packets === 0) {
        toast.info(
          data.scoped_packets === 0
            ? 'No region-scoped packets are stored yet'
            : 'Every stored scoped packet is already explained by a known region'
        );
      } else if (data.results.length === 0) {
        toast.info(`No matching regions among ${data.candidates_tried.toLocaleString()} names`);
      } else {
        toast.success(
          `Found ${data.results.length} region${data.results.length === 1 ? '' : 's'} — review and add`
        );
      }
    } catch (err) {
      toast.error('Failed to guess regions', {
        description: err instanceof Error ? err.message : undefined,
      });
    } finally {
      setGuessing(false);
    }
  };

  const runImport = async () => {
    setImporting(true);
    try {
      const data = await api.importRegions(url.trim());
      setImported(data);
      if (data.names.length === 0) {
        toast.info(
          data.already_known.length > 0
            ? 'Everything found is already in Known Regions'
            : 'No region names found on that page'
        );
      }
    } catch (err) {
      toast.error('Failed to import regions', {
        description: err instanceof Error ? err.message : undefined,
      });
    } finally {
      setImporting(false);
    }
  };

  return (
    <div className="space-y-3 rounded-md border border-input bg-muted/20 p-3">
      <div className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-[0.625rem] uppercase tracking-wider text-muted-foreground font-medium">
            Guess regions from stored packets
          </span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => void runGuess()}
            disabled={guessing}
          >
            {guessing ? 'Guessing... (about 10s)' : 'Guess Regions'}
          </Button>
        </div>
        <p className="text-[0.8125rem] text-muted-foreground">
          Region names cannot be read back from traffic, only tested. This tries every 2- and
          3-letter code (airport codes, provinces, states), pairs such as onqc, and any names
          imported below against your stored region-scoped packets, and lists the names that explain
          at least two of them. A name matching two different packets by chance is about a
          one-in-four-billion event, so listed names are real. Names nobody guessed cannot be found.
        </p>
        {guess && guess.results.length > 0 && (
          <div className="space-y-1">
            <p className="text-xs text-muted-foreground">
              Tested {guess.tested_packets.toLocaleString()} unexplained packets against{' '}
              {guess.candidates_tried.toLocaleString()} names
              {guess.timed_out ? ' (stopped early at the time limit)' : ''}.
            </p>
            {guess.results.map((r) => (
              <div key={r.region} className="flex items-center gap-2 text-sm font-mono">
                <span>{r.region}</span>
                <span className="text-xs text-muted-foreground font-sans">
                  {r.hits} packet{r.hits === 1 ? '' : 's'} ({r.pct_of_tested.toFixed(0)}%)
                </span>
              </div>
            ))}
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="border-success/50 text-success hover:bg-success/10"
              onClick={() => onAddRegions(guess.results.map((r) => r.region))}
            >
              Add to Known Regions
            </Button>
          </div>
        )}
        {guess && guess.results.length === 0 && guess.tested_packets > 0 && (
          <p className="text-xs text-muted-foreground">
            Tested {guess.tested_packets.toLocaleString()} unexplained packets against{' '}
            {guess.candidates_tried.toLocaleString()} names; none matched twice.
          </p>
        )}
      </div>

      <div className="space-y-2 border-t border-border pt-3">
        <span className="text-[0.625rem] uppercase tracking-wider text-muted-foreground font-medium">
          Import regions from a website
        </span>
        <div className="flex gap-2">
          <Input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://… page or JSON listing regions"
            aria-label="Region list URL"
            onKeyDown={(e) => {
              if (e.key === 'Enter' && url.trim() && !importing) void runImport();
            }}
          />
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => void runImport()}
            disabled={importing || !url.trim()}
          >
            {importing ? 'Fetching...' : 'Fetch'}
          </Button>
        </div>
        <p className="text-[0.8125rem] text-muted-foreground">
          Fetches a public page (JSON, HTML, CSV or plain text) and lists the region-like names on
          it. Nothing is saved until you add them. Imported names are unverified, so use{' '}
          <em>Check against packets</em> to keep only the ones your traffic actually uses.
        </p>
        {imported && imported.names.length > 0 && (
          <div className="space-y-2">
            <p className="text-xs text-muted-foreground">
              {imported.names.length} new name{imported.names.length === 1 ? '' : 's'}
              {imported.already_known.length > 0
                ? ` (${imported.already_known.length} already known)`
                : ''}
              :
            </p>
            <div className="max-h-32 overflow-y-auto rounded border border-border bg-background p-2 text-sm font-mono break-words">
              {imported.names.join('  ')}
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={guessing}
                onClick={() => void runGuess(imported.names)}
              >
                {guessing ? 'Checking...' : 'Check against packets'}
              </Button>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => onAddRegions(imported.names)}
              >
                Add all without checking
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
