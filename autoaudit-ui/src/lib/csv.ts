/**
 * Client-side CSV export of already-fetched/filtered findings (B15 — no
 * server-side aggregation). UTF-8 BOM keeps Excel from mangling accented
 * text, mirroring the Streamlit console's utf-8-sig behaviour.
 */

const BOM = "﻿";

export function escapeCsvField(value: unknown): string {
  if (value === null || value === undefined) return "";
  const str = String(value);
  if (/[",\n\r]/.test(str)) {
    return `"${str.replace(/"/g, '""')}"`;
  }
  return str;
}

export function toCsv(
  rows: Array<Record<string, unknown>>,
  columns: string[],
): string {
  const header = columns.map(escapeCsvField).join(",");
  const body = rows
    .map((row) => columns.map((col) => escapeCsvField(row[col])).join(","))
    .join("\r\n");
  return `${header}\r\n${body}`;
}

export function downloadCsv(filename: string, csv: string): void {
  const blob = new Blob([BOM + csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
