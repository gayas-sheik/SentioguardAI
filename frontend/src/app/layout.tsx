import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: {
    default: "SentioguardAI | Emotion-Controlled Video Generation",
    template: "%s | SentioguardAI",
  },
  description:
    "An AI-powered video generation platform for creating emotionally expressive talking-head videos using facial images, speech audio, and emotion control.",
  applicationName: "SentioguardAI",
  keywords: [
    "SentioguardAI",
    "emotion-controlled video generation",
    "talking-head video generation",
    "multimodal AI",
    "emotion processing",
    "video synthesis",
  ],
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#f5f7fb",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable}`}>
      <body>{children}</body>
    </html>
  );
}
