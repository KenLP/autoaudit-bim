import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
// Same `?raw` reason as App.codesplit.test.ts: this tsconfig types only
// `vite/client`, so a node:fs import type-checks under vitest and then fails
// `tsc -b`.
import SOURCE from "./FindingsTable.tsx?raw";
import { FindingsTable } from "./FindingsTable";
import type { Finding } from "@/api/types";

/**
 * The virtualized branch collapsed the whole table, and jsdom cannot see it.
 *
 * What shipped: rows were `position:absolute` inside a `display:block` tbody.
 * With every row out of flow the tbody had no in-flow content, collapsed to the
 * 32px checkbox column, and each row's `width:100%` resolved to 32px — so
 * `table-fixed` gave the remaining columns 0px and the element id, the bucket
 * badge and the severity badge all painted at the same x. Measured in a real
 * browser 2026-07-31: tbody 32px against a 1152px table, every data cell 0px,
 * at 1400px viewport as well as at panel width. It fired on every run with more
 * than 200 findings and on nothing else.
 *
 * jsdom reports 0×0 for everything, so the virtualizer computes an empty
 * viewport and renders NO rows — stubbing getBoundingClientRect, clientHeight
 * and ResizeObserver did not change that. That is also exactly how this branch
 * came to have no coverage at all, and how FE-9 shipped its own >200 bug.
 *
 * So the layout is verified by measuring in a browser, and this file pins the
 * SOURCE — the same blunt, honest instrument App.codesplit.test.ts uses: it
 * fails on precisely the edit that would bring the pattern back.
 */

function makeFindings(n: number): Finding[] {
  return Array.from({ length: n }, (_, i) => ({
    element_id: String(100000 + i),
    rule_id: "demo.doors.fire_rating",
    bucket: "non_compliant",
    severity: "severity_high",
    parameter: "Fire Rating",
    value: "180 MIN",
    suggested_value: "3 HR",
    message: "",
  })) as unknown as Finding[];
}

describe("FindingsTable — the virtualized branch stays inside the table layout", () => {
  it("never takes a row out of flow, and never makes the tbody a block", () => {
    // Each of these is one half of the collapse: out-of-flow rows leave the
    // tbody empty, a block tbody stops being a table-row-group and shrinks.
    expect(SOURCE).not.toMatch(/position:\s*["']absolute["']/);
    expect(SOURCE).not.toMatch(/display:\s*["']block["']/);
    expect(SOURCE).not.toMatch(/display:\s*["']table["']/);
    expect(SOURCE).not.toMatch(/tableLayout/);
    // `width: "100%"` on a row is what turned the collapsed tbody into
    // collapsed columns.
    expect(SOURCE).not.toMatch(/width:\s*["']100%["']/);
  });

  it("reserves the scroll height with spacer rows instead", () => {
    expect(SOURCE).toContain('data-testid="findings-spacer-top"');
    expect(SOURCE).toContain('data-testid="findings-spacer-bottom"');
    // The spacers must span the table, or the reserved height is applied to
    // one column and the scrollbar stops matching the data.
    expect(SOURCE).toMatch(/colSpan=\{columnCount\}/);
    expect(SOURCE).toMatch(/const padTop\b/);
    expect(SOURCE).toMatch(/const padBottom\b/);
  });

  it("counts the checkbox column, which the old colSpan forgot", () => {
    // compact renders checkbox+element+bucket+severity = 4; the full table adds
    // parameter+value+suggested = 7, plus rule = 8. The previous expression
    // (3 / 6 / 7) was short by exactly the checkbox column.
    expect(SOURCE).toMatch(/compact \? 4 : showRuleColumn \? 8 : 7/);
  });

  it("still renders a plain tbody below the virtualization threshold", () => {
    const { container } = render(
      <FindingsTable
        findings={makeFindings(5)}
        onSelect={() => {}}
        checkedKeys={new Set()}
        onToggleChecked={() => {}}
      />,
    );
    const tbody = container.querySelector("tbody")!;
    expect(tbody.getAttribute("style")).toBeNull();
    expect(screen.getAllByTestId("findings-row")).toHaveLength(5);
    expect(
      container.querySelector('[data-testid="findings-spacer-bottom"]'),
    ).toBeNull();
  });
});
