import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Bass Tab Transcription",
  description: "Upload audio, get a bass tab. Separation and pitch tracking on Runpod Serverless.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
