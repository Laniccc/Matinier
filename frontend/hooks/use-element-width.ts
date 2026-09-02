import { useEffect, useState } from "react";
import type { RefObject } from "react";

export function useElementWidth<T extends Element>(
  ref: RefObject<T | null>,
  fallbackWidth: number,
): number {
  if (!Number.isFinite(fallbackWidth) || fallbackWidth <= 0) {
    throw new Error("fallbackWidth must be a positive finite number");
  }
  const [width, setWidth] = useState(fallbackWidth);

  useEffect(() => {
    const element = ref.current;
    if (element === null) {
      return;
    }

    const updateWidth = (nextWidth: number) => {
      if (Number.isFinite(nextWidth) && nextWidth > 0) {
        setWidth(nextWidth);
      }
    };
    updateWidth(element.getBoundingClientRect().width);

    const observer = new ResizeObserver(([entry]) => {
      if (entry !== undefined) {
        updateWidth(entry.contentRect.width);
      }
    });
    observer.observe(element);
    return () => observer.disconnect();
  });

  return width;
}
