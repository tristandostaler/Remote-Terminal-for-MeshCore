import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { Loader2, Search } from 'lucide-react';

/**
 * Giphy search/trending picker for the composer, mirroring meshcore-open's
 * lib/widgets/gif_picker.dart: trending on open, search on submit, 25 results
 * rated G, a tap picks the GIF id. The caller turns the id into the `g:<id>`
 * wire payload (see meshcoreOpenPayloads.ts).
 *
 * The key is the public beta key meshcore-open ships with (limited usage).
 */
const GIPHY_API_KEY = 'sXpGFDGZs0Dv1mmNFvYaGUvYwKX0PWIh';
const GIPHY_RESULT_LIMIT = 25;
const GIPHY_TIMEOUT_MS = 10_000;

export interface GiphyResult {
  id: string;
  title: string;
  previewUrl: string | null;
}

interface GiphyApiGif {
  id?: unknown;
  title?: unknown;
  images?: { fixed_height_small?: { url?: unknown } };
}

function giphyEndpoint(query: string): string {
  const params = new URLSearchParams({
    api_key: GIPHY_API_KEY,
    limit: String(GIPHY_RESULT_LIMIT),
    rating: 'g',
  });
  const trimmed = query.trim();
  if (trimmed) params.set('q', trimmed);
  return `https://api.giphy.com/v1/gifs/${trimmed ? 'search' : 'trending'}?${params}`;
}

/** Fetch trending GIFs (empty query) or search results from Giphy. */
export async function fetchGiphy(query: string, signal?: AbortSignal): Promise<GiphyResult[]> {
  const response = await fetch(giphyEndpoint(query), { signal });
  if (!response.ok) throw new Error(`Giphy returned ${response.status}`);
  const body = (await response.json()) as { data?: GiphyApiGif[] };
  return (body.data ?? []).flatMap((gif) => {
    // Only ids that round-trip through the g:<id> wire format are offered.
    if (typeof gif.id !== 'string' || !/^[A-Za-z0-9_-]+$/.test(gif.id)) return [];
    const url = gif.images?.fixed_height_small?.url;
    return [
      {
        id: gif.id,
        title: typeof gif.title === 'string' ? gif.title : '',
        previewUrl: typeof url === 'string' ? url : null,
      },
    ];
  });
}

export function GifPickerPanel({ onPick }: { onPick: (gifId: string) => void }) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<GiphyResult[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastQuery, setLastQuery] = useState('');
  const abortRef = useRef<AbortController | null>(null);

  const load = useCallback((searchQuery: string) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), GIPHY_TIMEOUT_MS);
    setLoading(true);
    setError(null);
    setLastQuery(searchQuery);
    fetchGiphy(searchQuery, controller.signal)
      .then((gifs) => {
        if (abortRef.current !== controller) return;
        setResults(gifs);
        setLoading(false);
      })
      .catch(() => {
        if (abortRef.current !== controller) return;
        setError(searchQuery.trim() ? 'Failed to search GIFs' : 'Failed to load GIFs');
        setLoading(false);
      })
      .finally(() => window.clearTimeout(timeout));
  }, []);

  useEffect(() => {
    load('');
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, [load]);

  const handleSearchKey = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== 'Enter') return;
    // The composer is itself a <form>: Enter here must search, not submit the
    // draft (implicit submission) or reach the composer's own Enter handling.
    event.preventDefault();
    event.stopPropagation();
    load(query);
  };

  return (
    <div className="flex flex-col gap-2">
      {/* Not a nested <form> (invalid inside the composer's form): Enter is
          handled on the input instead. */}
      <div className="relative">
        <Search
          className="pointer-events-none absolute left-2 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
          aria-hidden="true"
        />
        <input
          type="search"
          aria-label="Search GIFs"
          placeholder="Search GIFs..."
          value={query}
          autoFocus
          onChange={(event) => {
            setQuery(event.target.value);
            // Clearing the box goes back to trending, as meshcore-open does.
            if (event.target.value === '' && lastQuery !== '') load('');
          }}
          onKeyDown={handleSearchKey}
          className="w-full rounded-md border border-input bg-background py-1.5 pl-8 pr-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </div>
      <div className="h-64 overflow-y-auto">
        {loading ? (
          <div className="flex h-full items-center justify-center">
            <Loader2 className="animate-spin text-muted-foreground" size={22} />
          </div>
        ) : error ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
            <span>{error}</span>
            <button
              type="button"
              className="rounded-md border border-border px-2 py-1 text-xs hover:bg-accent"
              onClick={() => load(lastQuery)}
            >
              Retry
            </button>
          </div>
        ) : results.length === 0 ? (
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            No GIFs found
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-1.5">
            {results.map((gif) => (
              <button
                key={gif.id}
                type="button"
                className="aspect-square overflow-hidden rounded-md bg-muted transition-opacity hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                aria-label={gif.title ? `Send GIF: ${gif.title}` : 'Send GIF'}
                title={gif.title || undefined}
                onClick={() => onPick(gif.id)}
              >
                {gif.previewUrl ? (
                  <img
                    src={gif.previewUrl}
                    alt=""
                    loading="lazy"
                    className="h-full w-full object-cover"
                  />
                ) : (
                  <span className="text-xs font-bold text-muted-foreground">GIF</span>
                )}
              </button>
            ))}
          </div>
        )}
      </div>
      <div className="text-[0.6875rem] text-muted-foreground">Powered by GIPHY</div>
    </div>
  );
}
