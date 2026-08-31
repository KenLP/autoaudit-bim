import { describe, expect, it } from "vitest";
import { computeCanSave } from "./canSave";

const GREEN = {
  basicMissing: [] as unknown[],
  blockingErrors: [] as unknown[],
  validationIsCurrent: true,
  legacyBanner: false,
};

describe("computeCanSave", () => {
  it("enables Save only when everything is green", () => {
    expect(computeCanSave(GREEN)).toBe(true);
  });

  it("blocks Save until the CURRENT draft has been validated (M5)", () => {
    // The undefined-first-paint and stale-pass windows: basic fields are
    // filled and there are no blocking errors, but the server has NOT
    // validated this exact draft yet. Deleting the validationIsCurrent term
    // from computeCanSave makes this assertion fail — which is the point.
    expect(computeCanSave({ ...GREEN, validationIsCurrent: false })).toBe(false);
  });

  it("blocks Save on missing basic fields", () => {
    expect(computeCanSave({ ...GREEN, basicMissing: ["Rule ID is required"] })).toBe(false);
  });

  it("blocks Save on server validation errors", () => {
    expect(computeCanSave({ ...GREEN, blockingErrors: [{ field: "threshold" }] })).toBe(false);
  });

  it("blocks Save for a legacy (read-only) rule", () => {
    expect(computeCanSave({ ...GREEN, legacyBanner: true })).toBe(false);
  });
});
