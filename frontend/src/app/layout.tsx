import type { Metadata, Viewport } from 'next';
import { Inter, JetBrains_Mono } from 'next/font/google';
import type { ReactNode } from 'react';

import { color } from '@/design/tokens';

import './globals.css';

const inter = Inter({ subsets: ['latin'], variable: '--font-inter', display: 'swap' });
const mono = JetBrains_Mono({ subsets: ['latin'], variable: '--font-mono', display: 'swap' });

export const metadata: Metadata = {
  title: 'Dhaka Tesla Pool',
  description: 'Pooled electric rides on the Banani, Gulshan and Mohakhali corridor: passenger console and driver cockpit.',
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  themeColor: color['canvas-base'],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${mono.variable}`}>
      <body>{children}</body>
    </html>
  );
}
