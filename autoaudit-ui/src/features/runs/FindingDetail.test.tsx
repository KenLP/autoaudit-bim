import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { TooltipProvider } from "@/components/ui/tooltip";
import { FindingDetail } from "./FindingDetail";
import type { Finding } from "@/api/types";

/** The shape the Diagnostic agent actually puts on the wire — copied from
 *  run-50d78cf2's `/api/runs/<id>/outcomes`, 2026-09-14. */
const AGENT_DIAGNOSIS = {
  summary:
    "Door leaf width is 863.6 mm, below the 900 mm minimum required. This is a nominal leaf width measurement, not clear opening, and affects egress safety.",
  suggested_action:
    "Verify door type and location. If on an egress route, consult life safety requirements and building code before adjusting.",
  source: "llm",
  confidence: 0.92,
};

function base(extra: Partial<Finding> = {}): Finding {
  return {
    rule_id: "snowdon.doors.width_min",
    element_id: 1055444,
    parameter: "Width",
    value: "863.6 mm",
    status: "non_compliant",
    bucket: "non_compliant",
    severity: "high",
    ...extra,
  } as Finding;
}

const noop = vi.fn();

/** The panel's disabled Highlight button is wrapped in a Tooltip, which Radix
 *  refuses to render outside a provider — AppShell supplies one in the app. */
function show(finding: Finding) {
  return render(
    <TooltipProvider>
      <FindingDetail finding={finding} revitStatus="error" onHighlight={noop} />
    </TooltipProvider>,
  );
}

describe("FindingDetail diagnosis", () => {
  it("renders the agent's object shape as prose, never [object Object]", () => {
    show(base({ diagnosis: AGENT_DIAGNOSIS }));

    const block = screen.getByTestId("finding-diagnosis");
    // The regression this pins: the panel typed `diagnosis` as a string and
    // rendered String(value), so every LLM diagnosis printed "[object Object]".
    expect(block.textContent).not.toContain("[object Object]");
    expect(block).toHaveTextContent(/863\.6 mm, below the 900 mm minimum/);
    expect(block).toHaveTextContent(/consult life safety requirements/);
    expect(block).toHaveTextContent(/confidence 0\.92/);
  });

  it("still renders a bare string diagnosis from an older run", () => {
    show(base({ diagnosis: "Checked by hand on 2026-08-01." }));
    expect(screen.getByTestId("finding-diagnosis")).toHaveTextContent(
      "Checked by hand on 2026-08-01.",
    );
  });

  it("shows no diagnosis block when the run had no LLM layer", () => {
    show(base());
    expect(screen.queryByTestId("finding-diagnosis")).toBeNull();
  });

  it("treats an empty object as no diagnosis rather than an empty box", () => {
    show(base({ diagnosis: { source: "llm" } }));
    expect(screen.queryByTestId("finding-diagnosis")).toBeNull();
  });
});
