import { describe, expect, it } from "vitest";
import { escapeCsvField, toCsv } from "./csv";

describe("escapeCsvField", () => {
  it("passes through plain values", () => {
    expect(escapeCsvField("447121")).toBe("447121");
  });

  it("quotes and doubles internal quotes when a comma is present", () => {
    expect(escapeCsvField('120 Min, "rated"')).toBe('"120 Min, ""rated"""');
  });

  it("quotes values containing newlines", () => {
    expect(escapeCsvField("line1\nline2")).toBe('"line1\nline2"');
  });

  it("renders null/undefined as empty string", () => {
    expect(escapeCsvField(null)).toBe("");
    expect(escapeCsvField(undefined)).toBe("");
  });
});

describe("toCsv", () => {
  it("builds a header + CRLF-joined body from column order", () => {
    const csv = toCsv(
      [
        { element_id: 447121, parameter: "Fire Rating", value: "" },
        { element_id: 447122, parameter: "Fire Rating", value: "120 Min" },
      ],
      ["element_id", "parameter", "value"],
    );
    expect(csv).toBe(
      "element_id,parameter,value\r\n447121,Fire Rating,\r\n447122,Fire Rating,120 Min",
    );
  });
});
