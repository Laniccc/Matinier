import type { TextWidthMeasurer } from "./caption-cues";

const MAX_CACHE_ENTRIES = 1_000;

export function createCanvasTextMeasurer(font: string): TextWidthMeasurer {
  if (typeof document === "undefined") {
    return (text) => Array.from(text).reduce(
      (width, part) => width + (/\p{Script=Han}/u.test(part) ? 13 : 7),
      0,
    );
  }
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d");
  const cache = new Map<string, number>();

  if (context === null) {
    return (text) => Array.from(text).length;
  }
  context.font = font;

  return (text) => {
    const cached = cache.get(text);
    if (cached !== undefined) {
      return cached;
    }
    const width = context.measureText(text).width;
    if (cache.size >= MAX_CACHE_ENTRIES) {
      cache.clear();
    }
    cache.set(text, width);
    return width;
  };
}
