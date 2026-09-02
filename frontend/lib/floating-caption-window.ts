export type FloatingCaptionWindowErrorCode =
  | "unsupported"
  | "not-allowed"
  | "not-supported"
  | "open-failed";

const ERROR_MESSAGES: Record<FloatingCaptionWindowErrorCode, string> = {
  unsupported: "当前浏览器不支持始终置顶字幕，请使用桌面版 Chrome 或 Edge。",
  "not-allowed": "浏览器未允许打开悬浮字幕，请再次点击开关重试。",
  "not-supported": "浏览器已禁用画中画窗口，请检查浏览器设置。",
  "open-failed": "悬浮字幕窗口打开失败，请稍后重试。",
};

export class FloatingCaptionWindowError extends Error {
  readonly code: FloatingCaptionWindowErrorCode;

  constructor(code: FloatingCaptionWindowErrorCode) {
    super(ERROR_MESSAGES[code]);
    this.name = "FloatingCaptionWindowError";
    this.code = code;
  }
}

function errorName(value: unknown): string | null {
  if (typeof value !== "object" || value === null || !("name" in value)) {
    return null;
  }
  return typeof value.name === "string" ? value.name : null;
}

export async function requestFloatingCaptionWindow(
  host: Window,
): Promise<{ window: Window; reused: boolean }> {
  const pictureInPicture = host.documentPictureInPicture;
  if (pictureInPicture === undefined) {
    throw new FloatingCaptionWindowError("unsupported");
  }

  const existing = pictureInPicture.window;
  if (existing !== null && !existing.closed) {
    existing.focus();
    return { window: existing, reused: true };
  }

  try {
    const opened = await pictureInPicture.requestWindow({
      width: 720,
      height: 180,
    });
    return { window: opened, reused: false };
  } catch (error) {
    const name = errorName(error);
    if (name === "NotAllowedError") {
      throw new FloatingCaptionWindowError("not-allowed");
    }
    if (name === "NotSupportedError") {
      throw new FloatingCaptionWindowError("not-supported");
    }
    throw new FloatingCaptionWindowError("open-failed");
  }
}
