import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * fork (itsdmd/SoulSync): long lists render a window of their rows and grow it
 * on demand, instead of putting every row of a thousand-item list into the
 * page (the tab froze on the wishlist and on the downloads review lists).
 *
 * The window grows by itself when the "Show more" row scrolls into view, so
 * scrolling feels like one long list; the button is the fallback.
 */
export const PROGRESSIVE_STEP = 60;

export function useProgressive<T>(
  items: readonly T[],
  resetKey: unknown = '',
  step = PROGRESSIVE_STEP,
) {
  const [count, setCount] = useState(step);
  // a new filter / sort / folder starts from the top again
  useEffect(() => {
    setCount(step);
  }, [resetKey, step]);
  const total = items.length;
  const shown = total > count ? items.slice(0, count) : (items as T[]);
  const more = useCallback(() => setCount((current) => current + step), [step]);
  return { shown, remaining: Math.max(0, total - count), total, more };
}

export function ProgressiveMore({
  remaining,
  onMore,
  noun = 'more',
}: {
  remaining: number;
  onMore: () => void;
  noun?: string;
}) {
  const ref = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    const node = ref.current;
    if (!node || remaining <= 0 || typeof IntersectionObserver === 'undefined') return undefined;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) onMore();
      },
      { rootMargin: '600px 0px' },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [remaining, onMore]);
  if (remaining <= 0) return null;
  return (
    <button
      ref={ref}
      type="button"
      className="fork-progressive-more"
      onClick={onMore}
      data-testid="progressive-more"
    >
      Show more ({remaining.toLocaleString()} {noun})
    </button>
  );
}
