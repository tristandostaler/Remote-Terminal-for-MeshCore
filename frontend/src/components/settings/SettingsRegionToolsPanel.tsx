import { useEffect, useState } from 'react';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { toast } from '../ui/sonner';
import { api } from '../../api';
import type { GuessRegionsJob, ImportRegionsResponse } from '../../types';

/**
 * Two ways to fill Known Regions without asking a repeater: brute-force names
 * against stored region-scoped packets, and import a list published on a website.
 *
 * A packet's region code is a one-way hash, so "brute force" tests candidate names
 * (every a-z name within a configurable letter range, plus anything imported
 * here) and reports the ones that explain several stored packets.
 */
// Region names are at most 30 characters, so that is the only bound on the range.
const MAX_LETTERS = 30;
const POLL_INTERVAL_MS = 1000;

const clampLetters = (value: string, fallback: number) => {
  const n = Number.parseInt(value, 10);
  return Number.isNaN(n) ? fallback : Math.min(MAX_LETTERS, Math.max(1, n));
};

const STATUS_LABEL: Record<GuessRegionsJob['status'], string> = {
  running: 'Running',
  completed: 'Finished',
  timed_out: 'Stopped at the time limit',
  cancelled: 'Stopped',
  failed: 'Failed',
};

const formatDuration = (seconds: number) => {
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  return h > 0 ? `${h}h ${m}m ${sec}s` : m > 0 ? `${m}m ${sec}s` : `${sec}s`;
};

export function RegionToolsPanel({ onAddRegions }: { onAddRegions: (names: string[]) => void }) {
  const [minLetters, setMinLetters] = useState(2);
  const [maxLetters, setMaxLetters] = useState(3);
  const [maxSeconds, setMaxSeconds] = useState(60);
  const [job, setJob] = useState<GuessRegionsJob | null>(null);
  const [starting, setStarting] = useState(false);
  const guessing = starting || job?.status === 'running';
  const [url, setUrl] = useState('');
  const [importing, setImporting] = useState(false);
  const [imported, setImported] = useState<ImportRegionsResponse | null>(null);

  const announceFinished = (data: GuessRegionsJob) => {
    if (data.status === 'failed') {
      toast.error('Region brute force failed', { description: data.error ?? undefined });
    } else if (data.tested_packets === 0) {
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
  };

  // The sweep runs on the server; this only polls for progress. A sweep already
  // running when the panel opens (e.g. after a reload) is picked back up.
  useEffect(() => {
    let cancelled = false;
    api
      .getGuessRegionsJob()
      .then((data) => {
        if (!cancelled && data?.status === 'running') setJob(data);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const runningJobId = job?.status === 'running' ? job.job_id : null;
  useEffect(() => {
    if (!runningJobId) return;
    let cancelled = false;
    const timer = setInterval(() => {
      api
        .getGuessRegionsJob()
        .then((data) => {
          if (cancelled || !data || data.job_id !== runningJobId) return;
          setJob(data);
          if (data.status !== 'running') announceFinished(data);
        })
        .catch(() => {});
    }, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [runningJobId]);

  const runGuess = async (candidates: string[] = []) => {
    setStarting(true);
    try {
      setJob(await api.startGuessRegions(candidates, minLetters, maxLetters, maxSeconds));
    } catch (err) {
      toast.error('Failed to start the region brute force', {
        description: err instanceof Error ? err.message : undefined,
      });
    } finally {
      setStarting(false);
    }
  };

  const cancelGuess = async () => {
    if (!job) return;
    try {
      setJob(await api.cancelGuessRegions(job.job_id));
    } catch (err) {
      toast.error('Failed to stop the brute force', {
        description: err instanceof Error ? err.message : undefined,
      });
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

  const loadFromLiveFeed = async () => {
    setImporting(true);
    try {
      const data = await api.getLiveFeedRegions();
      // Lowercase and dedupe: region names are conventionally lowercase and their
      // hash is case-sensitive, so the live feed's uppercase IATA codes would never match.
      const names = [...new Set(data.regions.map((r) => r.code?.trim().toLowerCase()))].filter(
        (n): n is string => !!n
      );
      setImported({ url: data.url, names, already_known: [] });
      if (names.length === 0) toast.info('The live feed reported no regions');
    } catch (err) {
      toast.error('Failed to load regions from the live feed', {
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
            Brute-force regions from stored packets
          </span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => void runGuess()}
            disabled={guessing}
          >
            {guessing ? 'Brute forcing...' : 'Brute Force Regions'}
          </Button>
          {job?.status === 'running' && (
            <Button type="button" variant="outline" size="sm" onClick={() => void cancelGuess()}>
              Stop
            </Button>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <label className="flex items-center gap-1">
            Min letters
            <Input
              type="number"
              min={1}
              max={MAX_LETTERS}
              className="w-16"
              value={minLetters}
              onChange={(e) => {
                const min = clampLetters(e.target.value, minLetters);
                setMinLetters(min);
                if (min > maxLetters) setMaxLetters(min);
              }}
            />
          </label>
          <label className="flex items-center gap-1">
            Max letters
            <Input
              type="number"
              min={1}
              max={MAX_LETTERS}
              className="w-16"
              value={maxLetters}
              onChange={(e) => {
                const max = clampLetters(e.target.value, maxLetters);
                setMaxLetters(max);
                if (max < minLetters) setMinLetters(max);
              }}
            />
          </label>
          <label className="flex items-center gap-1">
            Time limit (s, 0 = none)
            <Input
              type="number"
              min={0}
              className="w-24"
              value={maxSeconds}
              onChange={(e) => {
                const n = Number.parseInt(e.target.value, 10);
                setMaxSeconds(Number.isNaN(n) ? 0 : Math.max(0, n));
              }}
            />
          </label>
        </div>
        <p className="text-[0.8125rem] text-muted-foreground">
          Region names cannot be read back from traffic, only tested. This tries every a-z name from{' '}
          {minLetters} to {maxLetters} letters, and any names imported below, against your stored
          region-scoped packets, and lists the names that explain at least two of them. A name
          matching two different packets by chance is about a one-in-four-billion event, so listed
          names are real. Each extra letter multiplies the work by 26, so long ranges can take a
          very long time: the sweep runs on the server in the background, so you can leave this page
          and come back, and it stops at the time limit (0 = run until it finishes or you press
          Stop). Names with digits or hyphens cannot be found this way.
        </p>
        {job && (
          <div className="space-y-1">
            <p className="text-xs text-muted-foreground">
              {STATUS_LABEL[job.status]}: tried {job.candidates_tried.toLocaleString()} of{' '}
              {job.candidates_total.toLocaleString()} names against{' '}
              {job.tested_packets.toLocaleString()} unexplained packets in{' '}
              {formatDuration(job.elapsed_seconds)}.
            </p>
            {job.results.map((r) => (
              <div key={r.region} className="flex items-center gap-2 text-sm font-mono">
                <span>{r.region}</span>
                <span className="text-xs text-muted-foreground font-sans">
                  {r.hits} packet{r.hits === 1 ? '' : 's'} ({r.pct_of_tested.toFixed(0)}%)
                </span>
              </div>
            ))}
            {job.results.length > 0 && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="border-success/50 text-success hover:bg-success/10"
                onClick={() => onAddRegions(job.results.map((r) => r.region))}
              >
                Add to Known Regions
              </Button>
            )}
          </div>
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
        <div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => void loadFromLiveFeed()}
            disabled={importing}
          >
            Load from live feed
          </Button>
          <span className="ml-2 text-xs text-muted-foreground">
            The regions (IATA codes) known to your configured live feed instance, e.g.
            live.meshcore.ca.
          </span>
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
