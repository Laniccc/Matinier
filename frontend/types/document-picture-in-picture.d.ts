export {};

declare global {
  type DocumentPictureInPictureOptions = {
    width?: number;
    height?: number;
    disallowReturnToOpener?: boolean;
    preferInitialWindowPlacement?: boolean;
  };

  interface DocumentPictureInPicture {
    readonly window: Window | null;
    requestWindow(options?: DocumentPictureInPictureOptions): Promise<Window>;
  }

  interface Window {
    readonly documentPictureInPicture?: DocumentPictureInPicture;
  }
}
