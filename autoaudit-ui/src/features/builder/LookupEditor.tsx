import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { strings } from "@/strings";
import { useLookups, useSaveLookup } from "@/api/hooks";
import type { LookupKeyEntry, LookupRowEntry } from "@/api/types";
import { toast } from "sonner";

/** `param : dimension` per line — mirrors app.py `_parse_lookup_keys`. */
function parseKeys(raw: string): LookupKeyEntry[] {
  const out: LookupKeyEntry[] = [];
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const [param, dimRaw] = trimmed.split(":");
    const dim = (dimRaw ?? "").trim().toLowerCase();
    if (param?.trim()) {
      out.push({
        param: param.trim(),
        dimension: dim === "fire_rating" ? "fire_rating" : "string",
      });
    }
  }
  return out;
}

/** `w1 | w2 | ... -> required` per line — mirrors app.py `_parse_lookup_rows`. */
function parseRows(raw: string): LookupRowEntry[] {
  const out: LookupRowEntry[] = [];
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || !trimmed.includes("->")) continue;
    const [lhs, require] = trimmed.split("->");
    const when = lhs.split("|").map((w) => w.trim());
    const req = require.trim();
    if (req && when.every((w) => w !== "")) {
      out.push({ when, require: req });
    }
  }
  return out;
}

export interface LookupEditorProps {
  onSaved: (name: string) => void;
}

/** Inline "create a new lookup table" editor (spec 3b M2: relation_compare
 *  lookup mode). Text-line editing (not a spreadsheet grid) mirrors the
 *  Streamlit Rule Builder's proven UX for transcribing a code table. */
export function LookupEditor({ onSaved }: LookupEditorProps) {
  const { data } = useLookups();
  const save = useSaveLookup();
  const [name, setName] = useState("");
  const [keysRaw, setKeysRaw] = useState(
    "host.Fire Rating : fire_rating\nhost.Fire Function : string",
  );
  const [rowsRaw, setRowsRaw] = useState(
    "1 HR | Corridor -> 20 min\n1 HR | * -> 60 min\n2 HR | * -> 90 min\n3 HR | * -> 3 HR",
  );
  const [confirmOverwrite, setConfirmOverwrite] = useState(false);

  const keys = parseKeys(keysRaw);
  const rows = parseRows(rowsRaw);
  const existingNames = new Set((data?.lookups ?? []).map((l) => l.name));
  const errors: string[] = [];
  if (!name.trim()) errors.push("Table name required");
  if (keys.length === 0) errors.push("Need at least 1 key");
  if (rows.length === 0) errors.push("Need at least 1 valid row (… -> required)");
  const mismatched = rows.filter((r) => r.when.length !== keys.length);
  if (keys.length && mismatched.length) errors.push(`${mismatched.length} row(s) have the wrong column count`);

  function doSave(overwrite: boolean) {
    save.mutate(
      { name: name.trim(), body: { keys, rows, overwrite } },
      {
        onSuccess: () => {
          toast(`Saved lookup.${name.trim()}.yaml`);
          onSaved(name.trim());
          setConfirmOverwrite(false);
        },
        onError: (err) => toast.error(String(err)),
      },
    );
  }

  function handleSave() {
    if (existingNames.has(name.trim())) {
      setConfirmOverwrite(true);
      return;
    }
    doSave(false);
  }

  return (
    <div className="flex flex-col gap-2 rounded-[var(--radius)] border border-[var(--border)] p-2">
      <label className="flex flex-col gap-1">
        <span className="text-caption">Table name (slug)</span>
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="ibc716" />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-caption">Keys — one `param : dimension` per line</span>
        <Textarea rows={2} value={keysRaw} onChange={(e) => setKeysRaw(e.target.value)} className="font-mono-val" />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-caption">Rows — `value1 | value2 | … -&gt; required` (* = wildcard)</span>
        <Textarea rows={4} value={rowsRaw} onChange={(e) => setRowsRaw(e.target.value)} className="font-mono-val" />
      </label>
      {errors.length > 0 && (
        <ul className="text-caption text-[var(--warn)]">
          {errors.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      )}
      <Button
        size="sm"
        className="w-fit"
        disabled={errors.length > 0 || save.isPending}
        onClick={handleSave}
      >
        {strings.common.save}
      </Button>

      <ConfirmDialog
        open={confirmOverwrite}
        onOpenChange={setConfirmOverwrite}
        title={strings.builder.overwriteTitle}
        description={`config/lookup.${name.trim()}.yaml already exists with different content. Overwrite it?`}
        loading={save.isPending}
        onConfirm={() => doSave(true)}
      />
    </div>
  );
}
