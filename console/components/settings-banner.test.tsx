import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SettingsBanner } from "@/components/app-shell";

describe("the settings banner", () => {
  it("says new calls are refused while the assistant is off", () => {
    render(<SettingsBanner calls="off" />);

    expect(screen.getByRole("status").textContent).toContain(
      "The phone assistant is switched off. New calls are refused.",
    );
  });

  it("says the settings could not be read, and not that calls are refused, when they are unknown", () => {
    render(<SettingsBanner calls={null} />);

    const notice = screen.getByRole("status").textContent;
    expect(notice).toContain("The assistant's settings could not be read just now.");
    expect(notice).not.toContain("switched off");
  });

  it("shows nothing while the assistant is on", () => {
    const { container } = render(<SettingsBanner calls="on" />);

    expect(container.textContent).toBe("");
  });
});
