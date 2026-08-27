import { useState } from 'react';
import { X } from 'lucide-react';
import { QUICK_REACTION_EMOJIS, REACTION_EMOJI_CATEGORIES } from '../utils/meshcoreOpenPayloads';

/**
 * The MeshCore Open Advanced emoji table as a picker: the quick row first,
 * then — behind the ⋯ toggle — the scrollable categorized grid of every
 * choice. Shared by the message-actions dialog (reactions) and the composer's
 * "+" tray (inserting into a draft) so both surfaces offer the same emojis in
 * the same order.
 */
export function EmojiPickerPanel({
  onPick,
  emojiLabel,
}: {
  onPick: (emoji: string) => void;
  /** aria-label for one emoji button, e.g. `React with 👍` or `Insert 👍`. */
  emojiLabel: (emoji: string) => string;
}) {
  const [showAllEmojis, setShowAllEmojis] = useState(false);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-1">
        {QUICK_REACTION_EMOJIS.map((emoji) => (
          <button
            key={emoji}
            type="button"
            className="flex h-9 w-9 items-center justify-center rounded-full text-xl transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            aria-label={emojiLabel(emoji)}
            onClick={() => onPick(emoji)}
          >
            {emoji}
          </button>
        ))}
        <button
          type="button"
          className="flex h-9 w-9 items-center justify-center rounded-full text-sm text-muted-foreground transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-label={showAllEmojis ? 'Hide emoji list' : 'More emojis'}
          aria-expanded={showAllEmojis}
          onClick={() => setShowAllEmojis((prev) => !prev)}
        >
          {showAllEmojis ? <X className="h-4 w-4" /> : '⋯'}
        </button>
      </div>
      {showAllEmojis && (
        <div className="max-h-48 overflow-y-auto rounded-md border border-border p-2">
          {REACTION_EMOJI_CATEGORIES.map((category) => (
            <div key={category.label}>
              <div className="px-1 pb-1 pt-2 text-xs font-medium text-muted-foreground first:pt-0">
                {category.label}
              </div>
              <div className="flex flex-wrap">
                {category.emojis.map((emoji, i) => (
                  <button
                    key={`${category.label}-${i}`}
                    type="button"
                    className="flex h-8 w-8 items-center justify-center rounded text-lg transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    aria-label={emojiLabel(emoji)}
                    onClick={() => onPick(emoji)}
                  >
                    {emoji}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
