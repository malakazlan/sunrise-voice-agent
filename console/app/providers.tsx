"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { setNonce } from "get-nonce";
import { useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";

import { ApiClient, ApiError } from "@/lib/api/client";
import { ApiProvider } from "@/lib/api/hooks";

// Worth another try: the network, a timeout or the API being briefly unavailable. Never a refusal,
// a 401, a 404 or an answer of the wrong shape.
const TRANSIENT = new Set(["network", "timeout", "unavailable"]);

export function Providers({
  apiOrigin,
  nonce,
  children,
}: {
  apiOrigin: string;
  nonce: string;
  children: ReactNode;
}) {
  const router = useRouter();
  // The scroll lock of the menu sheet and the select's viewport add a <style>; with this request's
  // nonce the CSP lets it apply. Set in the browser only, before either can open.
  useEffect(() => {
    setNonce(nonce);
  }, [nonce]);
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 15_000,
            retry: (failures, error) =>
              failures < 2 && error instanceof ApiError && TRANSIENT.has(error.kind),
          },
          mutations: { retry: false },
        },
      }),
  );
  const [client] = useState(
    () =>
      new ApiClient({
        origin: apiOrigin,
        onUnauthorized: () => {
          queryClient.clear();
          if (window.location.pathname !== "/login") {
            router.replace("/login");
          }
        },
      }),
  );

  return (
    <QueryClientProvider client={queryClient}>
      <ApiProvider client={client}>{children}</ApiProvider>
    </QueryClientProvider>
  );
}
