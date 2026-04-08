import type { ReactNode } from "react";

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body style={{ fontFamily: "Inter, Arial, sans-serif", margin: 24 }}>
        <nav style={{ display: "flex", gap: 16, marginBottom: 24 }}>
          <a href="/">Dashboard</a>
          <a href="/settings">Settings</a>
          <a href="/history">History</a>
        </nav>
        {children}
      </body>
    </html>
  );
}

