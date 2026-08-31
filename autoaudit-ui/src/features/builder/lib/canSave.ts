// M5 (2026-07 audit): the Save-enable decision, extracted so it is unit-tested
// in isolation. Inline in BuilderPage it was silently removable — no test
// exercised the `validationIsCurrent` term, so deleting the stale/first-paint
// guard would still pass the whole suite. Keeping it a pure function pins it.

export interface CanSaveInput {
  /** Client-side required-field gaps (id, parameter/category). */
  basicMissing: readonly unknown[];
  /** Server validation errors for the CURRENT draft. */
  blockingErrors: readonly unknown[];
  /**
   * True only when the server has validated THIS exact draft — not a previous
   * one (stale pass) and not "nothing yet" (undefined on first paint). This is
   * the term that stops Save from being authorized by a validation that
   * describes a different draft.
   */
  validationIsCurrent: boolean;
  /** A legacy (read-only) requirement is loaded → editing/saving is blocked. */
  legacyBanner: boolean;
}

export function computeCanSave(input: CanSaveInput): boolean {
  return (
    input.basicMissing.length === 0 &&
    input.blockingErrors.length === 0 &&
    input.validationIsCurrent &&
    !input.legacyBanner
  );
}
