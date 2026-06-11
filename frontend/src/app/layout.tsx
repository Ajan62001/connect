import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

import { Sidebar } from "@/components/shell/Sidebar";
import { Topbar } from "@/components/shell/Topbar";
import { Toaster } from "@/components/ui/sonner";

import { Providers } from "./providers";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "connect",
  description:
    "Information intelligence for Indian policy, politics, and finance.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
      suppressHydrationWarning
    >
      <head>
        {/* Opt out of Dark Reader's dynamic theming: it mutates the DOM before
            React hydrates, causing hydration attribute mismatches. */}
        <meta name="darkreader-lock" />
      </head>
      <body className="h-screen overflow-hidden">
        <Providers>
          <div className="flex h-full">
            <Sidebar />
            <div className="flex min-w-0 flex-1 flex-col">
              <Topbar />
              <main className="flex-1 overflow-y-auto">
                <div className="mx-auto w-full max-w-6xl space-y-6 p-6">
                  {children}
                </div>
              </main>
            </div>
          </div>
          <Toaster richColors position="bottom-right" />
        </Providers>
      </body>
    </html>
  );
}
