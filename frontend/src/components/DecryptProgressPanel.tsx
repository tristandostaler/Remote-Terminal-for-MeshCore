import type { DecryptSweepProgress } from '../types';

/**
 * Progress of a historical decrypt sweep: how far through the stored packets it
 * is, and what it has recovered so far, per conversation.
 *
 * The server throttles its ticks, so this renders whatever it is handed. A
 * finished sweep keeps its numbers on screen — "recovered 12 messages across 3
 * rooms" is the answer to the question the operator asked, and blanking it the
 * instant the scan ends would throw that away.
 */

function percentOf(progress: DecryptSweepProgress): number {
  if (progress.status === 'complete') {
    return 100;
  }
  if (progress.total <= 0) {
    return 0;
  }
  return Math.min(100, (progress.processed / progress.total) * 100);
}

function summarize(progress: DecryptSweepProgress): string {
  const found = progress.decrypted;
  const plural = found === 1 ? '' : 's';
  if (progress.status === 'failed') {
    return `Sweep failed after ${progress.processed.toLocaleString()} packets`;
  }
  if (progress.status === 'complete') {
    return found === 0
      ? 'Nothing decrypted with these keys'
      : `Recovered ${found.toLocaleString()} message${plural}`;
  }
  return found === 0
    ? 'No messages recovered yet'
    : `${found.toLocaleString()} message${plural} recovered so far`;
}

export function DecryptProgressPanel({
  progress,
  className,
  compact = false,
}: {
  progress: DecryptSweepProgress;
  className?: string;
  /** Drop the per-conversation breakdown, for the narrow info panes. */
  compact?: boolean;
}) {
  const percent = percentOf(progress);
  const running = progress.status === 'running' || progress.status === 'queued';

  return (
    <div className={className} data-testid="decrypt-progress">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-sm font-medium truncate" title={progress.label}>
          {running ? 'Decrypting' : 'Decrypt finished'}
          {progress.target_count > 1 ? ` · ${progress.label}` : ''}
        </span>
        <span className="text-xs text-muted-foreground tabular-nums whitespace-nowrap">
          {progress.processed.toLocaleString()} / {progress.total.toLocaleString()}
        </span>
      </div>

      <div
        className="mt-1.5 h-2 bg-muted rounded overflow-hidden"
        role="progressbar"
        aria-valuenow={Math.round(percent)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Historical decrypt progress"
      >
        <div
          className={`h-full transition-all duration-200 ${
            progress.status === 'failed' ? 'bg-destructive' : 'bg-primary'
          }`}
          style={{ width: `${percent}%` }}
        />
      </div>

      <p className="mt-1.5 text-xs text-muted-foreground">
        {summarize(progress)}
        {progress.queued > 0 &&
          ` · ${progress.queued} sweep${progress.queued === 1 ? '' : 's'} queued`}
      </p>

      {!compact && progress.targets.length > 0 && (
        <ul className="mt-2 space-y-0.5">
          {progress.targets.map((target) => (
            <li key={target.key} className="flex justify-between gap-2 text-xs">
              <span className="truncate text-foreground" title={target.name}>
                {target.name}
              </span>
              <span className="tabular-nums text-muted-foreground whitespace-nowrap">
                {target.decrypted.toLocaleString()} message
                {target.decrypted === 1 ? '' : 's'}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
