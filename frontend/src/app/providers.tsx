"use client";

import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { useUserSwitchCacheReset } from "@/lib/queries";

/**
 * Drops the whole query cache if the signed-in user changes within one JS
 * lifetime (per-user surfaces — watches, brief, today, spend, dossiers —
 * must never leak across a logout/login user switch). Renders nothing; it
 * just needs to live inside the QueryClientProvider.
 */
function UserSwitchBoundary() {
  useUserSwitchCacheReset();
  return null;
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            retry: 1,
            refetchOnWindowFocus: false,
            staleTime: 15_000,
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={queryClient}>
      <UserSwitchBoundary />
      {children}
    </QueryClientProvider>
  );
}
