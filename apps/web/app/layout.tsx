import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import { LiveRefresh } from "@/components/LiveRefresh";
import { Shell } from "@/components/Shell";
import "./globals.css";

const sans = IBM_Plex_Sans({ subsets: ["latin"], weight: ["400", "500"], variable: "--font-sans" });
const mono = IBM_Plex_Mono({ subsets: ["latin"], weight: ["400"], variable: "--font-mono" });

export const metadata: Metadata = {
  title: "Trading Engine",
  description: "Experimental quantitative crypto research desk",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="pt-BR">
      <body className={`${sans.variable} ${mono.variable} font-sans antialiased`}>
        <LiveRefresh />
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
