import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Providers } from "@/app/providers";
import { useCalls } from "@/lib/api/hooks";

const replace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
}));

function Reader() {
  useCalls(undefined);
  return null;
}

describe("the console's providers", () => {
  beforeEach(() => {
    replace.mockReset();
  });

  it("send the user to sign-in when the API answers 401", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(async () => Response.json({ detail: "Sign-in required" }, { status: 401 })),
    );

    render(
      <Providers apiOrigin="https://admin.clinic.test" nonce="test-nonce">
        <Reader />
      </Providers>,
    );

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
  });
});
