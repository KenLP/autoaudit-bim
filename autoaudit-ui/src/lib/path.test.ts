import { describe, expect, it } from "vitest";
import { basename } from "./path";

describe("basename", () => {
  it("extracts the file name from a windows path", () => {
    expect(basename("C:\\Users\\ken\\config\\rules.demo.yaml")).toBe("rules.demo.yaml");
  });

  it("extracts the file name from a posix path", () => {
    expect(basename("config/rules.demo.yaml")).toBe("rules.demo.yaml");
  });

  it("returns the input unchanged when there is no separator", () => {
    expect(basename("rules.demo.yaml")).toBe("rules.demo.yaml");
  });

  it("handles a trailing separator", () => {
    expect(basename("config/rules.demo.yaml/")).toBe("rules.demo.yaml");
  });

  it("handles an empty string", () => {
    expect(basename("")).toBe("");
  });
});
