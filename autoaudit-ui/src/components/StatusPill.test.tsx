import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { TooltipProvider } from "@/components/ui/tooltip";
import { StatusPill } from "./StatusPill";

function renderPill(status: "up" | "unconfigured" | "error" | "checking") {
  return render(
    <TooltipProvider>
      <StatusPill name="Revit" status={status} />
    </TooltipProvider>,
  );
}

describe("StatusPill", () => {
  it("renders the axis label for every health state", () => {
    (["up", "unconfigured", "error", "checking"] as const).forEach((status) => {
      const { unmount } = renderPill(status);
      expect(screen.getByText("Revit")).toBeInTheDocument();
      unmount();
    });
  });

  it("exposes the current status via data-status for styling/tests", () => {
    renderPill("up");
    expect(screen.getByTestId("status-pill-revit")).toHaveAttribute("data-status", "up");
  });

  it("switches data-status when the health state changes", () => {
    const { rerender } = render(
      <TooltipProvider>
        <StatusPill name="Forma" status="unconfigured" />
      </TooltipProvider>,
    );
    expect(screen.getByTestId("status-pill-forma")).toHaveAttribute(
      "data-status",
      "unconfigured",
    );
    rerender(
      <TooltipProvider>
        <StatusPill name="Forma" status="error" />
      </TooltipProvider>,
    );
    expect(screen.getByTestId("status-pill-forma")).toHaveAttribute("data-status", "error");
  });
});
