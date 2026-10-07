import "@testing-library/jest-dom/vitest";

// jsdom implements no Pointer Events API and no layout, so three methods
// Radix's Select calls on every open are simply missing — the dropdown throws
// `target.hasPointerCapture is not a function` before it can render an item.
// These are environment gaps, not component behaviour, so they are stubbed
// once here rather than per test file. Anything that depends on real geometry
// (positioning, scroll-into-view) is untestable in jsdom either way; what
// stays testable is what the tests actually assert — which options exist and
// what selecting one does.
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => {};
  Element.prototype.releasePointerCapture = () => {};
}
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}
if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}
