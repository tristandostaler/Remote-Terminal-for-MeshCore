import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

/**
 * Tooltips a phone can actually reach.
 *
 * Almost everything explanatory in this app hangs off a native `title`: what the
 * hop digits mean, why a message says 2/3, what the compression badge measured.
 * A mouse reveals those on hover and a touch screen reveals them never -- and
 * the one gesture that comes close, a long press, is already taken inside a
 * message bubble, where it opens the message-actions dialog. Reaching for the
 * tooltip on a timestamp got you the retry dialog instead.
 *
 * So a tap reveals the title, from one delegated listener rather than a wrapper
 * component at each of the ~200 title sites. The listener deliberately stays
 * out of the way of anything that already answers a press -- buttons, links,
 * `role="button"` spans -- because pressing those opens the detail they
 * describe, and a bubble on top of that is noise. What is left is inert text
 * carrying a title and nothing else, which is exactly the set that had no way
 * in. A long press on one of those now shows the tooltip too, instead of the
 * dialog that was never what the finger was after.
 *
 * The bubble is a portal at a fixed position rather than a sibling of its
 * trigger: the message list is virtualized, so anything rendered inside a row
 * gets clipped by the scroll container -- the same reason message actions are a
 * dialog and not an inline popover.
 */

const TOOLTIP_ID = 'tap-tooltip';

/**
 * Things that answer a press on their own. Also the reason this is a selector
 * and not a check for handlers: React attaches its listeners at the root
 * container, so there is nothing on the DOM node to look at.
 */
const INTERACTIVE_SELECTOR = [
  'a[href]',
  'button',
  'input',
  'select',
  'textarea',
  'summary',
  'label',
  '[role="button"]',
  '[role="link"]',
  '[role="switch"]',
  '[role="checkbox"]',
  '[role="tab"]',
  '[role="menuitem"]',
  '[role="option"]',
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

/**
 * The titled element a press should explain, or null when the press belongs to
 * something else. Exported because the rule -- not the bubble -- is the part
 * worth pinning down in tests.
 */
export function tapTooltipTarget(node: EventTarget | null): HTMLElement | null {
  if (!(node instanceof Element)) return null;
  const titled = node.closest<HTMLElement>('[title]');
  if (!titled) return null;
  if (!titled.getAttribute('title')?.trim()) return null;
  // An interactive ancestor between the press and the title -- or the titled
  // element itself being interactive -- means the press has a job already.
  if (node.closest(INTERACTIVE_SELECTOR)) return null;
  if (node.closest('[data-no-tap-tooltip]')) return null;
  return titled;
}

interface OpenTip {
  text: string;
  anchor: HTMLElement;
  /** Where the anchor was when it was pressed; the tip closes if anything moves. */
  rect: DOMRect;
}

interface Placement {
  left: number;
  top: number;
}

const VIEWPORT_MARGIN = 8;
const ANCHOR_GAP = 6;

export function TapTooltipLayer() {
  const [tip, setTip] = useState<OpenTip | null>(null);
  const [placement, setPlacement] = useState<Placement | null>(null);
  const bubbleRef = useRef<HTMLDivElement | null>(null);
  // The listeners are registered once, so what is open has to be readable from
  // a ref rather than closed over.
  const tipRef = useRef<OpenTip | null>(null);
  tipRef.current = tip;
  // A right-click keeps the desktop context menu; only a finger or a pen gets
  // the long-press tooltip, so the habit of right-clicking a bubble survives.
  const pointerTypeRef = useRef('mouse');
  // A long press ends in a pointerup too, and that release must not immediately
  // toggle shut the tooltip the press just opened.
  const consumedRef = useRef(false);

  const open = useCallback((anchor: HTMLElement) => {
    setTip({
      text: (anchor.getAttribute('title') ?? '').trim(),
      anchor,
      rect: anchor.getBoundingClientRect(),
    });
    setPlacement(null);
  }, []);

  useEffect(() => {
    const close = () => setTip(null);

    /** Open, or shut again if this is a second press on the same thing. */
    const toggle = (anchor: HTMLElement) => {
      if (tipRef.current?.anchor === anchor) close();
      else open(anchor);
    };

    const onPointerDown = (event: Event) => {
      pointerTypeRef.current = (event as PointerEvent).pointerType || 'mouse';
      consumedRef.current = false;
      // Pressing anywhere else dismisses. Pressing the same anchor again does
      // not, so that the release right behind this press can toggle it shut.
      if (tipRef.current && tapTooltipTarget(event.target) !== tipRef.current.anchor) close();
    };

    // A finger is served on pointerup rather than click, because iOS Safari does
    // not reliably deliver a click from a tap on an element nothing considers
    // clickable -- which is the whole set this module serves. A gesture that
    // turns into a scroll ends in pointercancel, so it never gets here.
    const onPointerUp = (event: Event) => {
      if (pointerTypeRef.current === 'mouse') return;
      if (consumedRef.current) {
        consumedRef.current = false;
        return;
      }
      const anchor = tapTooltipTarget(event.target);
      if (anchor) toggle(anchor);
    };

    const onClick = (event: MouseEvent) => {
      // Touch already had its turn on pointerup; the click that trails a tap
      // would only undo it.
      if (pointerTypeRef.current !== 'mouse') return;
      const anchor = tapTooltipTarget(event.target);
      if (anchor) toggle(anchor);
    };

    const onContextMenu = (event: MouseEvent) => {
      if (pointerTypeRef.current === 'mouse') return;
      const anchor = tapTooltipTarget(event.target);
      if (!anchor) return;
      // Capture phase, so the message bubble's own handler never runs: the
      // dialog it would open is the thing this whole module exists to stop.
      event.preventDefault();
      event.stopPropagation();
      consumedRef.current = true;
      open(anchor);
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
    };

    document.addEventListener('pointerdown', onPointerDown, true);
    document.addEventListener('pointerup', onPointerUp);
    document.addEventListener('click', onClick);
    document.addEventListener('contextmenu', onContextMenu, true);
    document.addEventListener('keydown', onKeyDown);
    // Capture, because the scroll that matters is usually an inner container's.
    document.addEventListener('scroll', close, { capture: true, passive: true });
    window.addEventListener('resize', close);

    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true);
      document.removeEventListener('pointerup', onPointerUp);
      document.removeEventListener('click', onClick);
      document.removeEventListener('contextmenu', onContextMenu, true);
      document.removeEventListener('keydown', onKeyDown);
      document.removeEventListener('scroll', close, true);
      window.removeEventListener('resize', close);
    };
  }, [open]);

  // Measure, then place: the bubble renders hidden at the origin for one frame
  // so its real height is known before it is put above or below the anchor.
  useLayoutEffect(() => {
    if (!tip) {
      setPlacement(null);
      return;
    }
    const bubble = bubbleRef.current;
    if (!bubble) return;
    const box = bubble.getBoundingClientRect();
    const rightEdge = Math.max(VIEWPORT_MARGIN, window.innerWidth - box.width - VIEWPORT_MARGIN);
    const left = Math.min(
      Math.max(VIEWPORT_MARGIN, tip.rect.left + tip.rect.width / 2 - box.width / 2),
      rightEdge
    );
    const above = tip.rect.top - box.height - ANCHOR_GAP;
    const bottomEdge = Math.max(VIEWPORT_MARGIN, window.innerHeight - box.height - VIEWPORT_MARGIN);
    const top =
      above >= VIEWPORT_MARGIN ? above : Math.min(tip.rect.bottom + ANCHOR_GAP, bottomEdge);
    setPlacement({ left, top });
  }, [tip]);

  // Say out loud what the tooltip describes, for as long as it is open.
  useEffect(() => {
    if (!tip) return;
    const { anchor } = tip;
    anchor.setAttribute('aria-describedby', TOOLTIP_ID);
    return () => {
      if (anchor.getAttribute('aria-describedby') === TOOLTIP_ID) {
        anchor.removeAttribute('aria-describedby');
      }
    };
  }, [tip]);

  if (!tip) return null;

  return createPortal(
    // pointer-events-none throughout: a tap that lands on the bubble should
    // reach whatever is underneath and dismiss, not be swallowed by it.
    <div className="pointer-events-none fixed inset-0 z-[200]">
      <div
        ref={bubbleRef}
        id={TOOLTIP_ID}
        role="tooltip"
        className="absolute max-w-[min(20rem,calc(100vw-1rem))] whitespace-normal break-words rounded-md border border-border bg-popover px-2 py-1 text-xs leading-snug text-popover-foreground shadow-md"
        style={{
          left: placement?.left ?? 0,
          top: placement?.top ?? 0,
          visibility: placement ? 'visible' : 'hidden',
        }}
      >
        {tip.text}
      </div>
    </div>,
    document.body
  );
}
