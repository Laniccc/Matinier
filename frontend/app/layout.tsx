import type { Metadata } from "next";

import "./globals.css";


export const metadata: Metadata = {
  title: "LiveCaption Studio",
  description: "Stage 0 LiveKit room connectivity",
};


export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}

