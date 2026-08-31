/**
 * Geometry-rule form (clearance checks).
 *
 * NOTE on the three category dropdowns: their SelectItem value is the category
 * LABEL ("Ducts"), not the catalog key ("ducts"). Rules store the display name,
 * and Radix Select matches values EXACTLY — keying on `c.key` left Category and
 * Reference category BLANK on any rule loaded or drafted into this form (seen
 * 2026-08-26 on the first NL-drafted geometry rule). The parameter Scope
 * section already carries the same fix.
 */
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Button } from "@/components/ui/button";
import { strings } from "@/strings";
import { useCategories } from "@/api/hooks";
import type { GeometryFormState } from "./lib/ruleForm";
import { useState } from "react";

export interface GeometryFormProps {
  state: GeometryFormState;
  onChange: (patch: Partial<GeometryFormState>) => void;
}

const CHECK_TYPES = [
  "clearance_min",
  "clearance_max",
  "spatial_containment",
  "min_spacing",
] as const;

const NEEDS_THRESHOLD = new Set(["clearance_min", "clearance_max", "min_spacing"]);
const NEEDS_DIRECTION = new Set(["clearance_min", "clearance_max"]);
const NEEDS_REFERENCE = new Set(["clearance_min", "clearance_max", "min_spacing"]);

/** Geometry rule capture form (item 4 — toggle at the top of the Builder
 *  page). Builds a `GeometryRuleDict` losslessly; it runs via the audit
 *  axes satellite pipeline (v1.4-K7), NOT the stale "does not run" claim
 *  the old Streamlit form used to show. */
export function GeometryForm({ state, onChange }: GeometryFormProps) {
  const { data: categoriesData } = useCategories();
  const categories = categoriesData?.categories ?? [];
  const [filterOpen, setFilterOpen] = useState(
    !!(state.spatialFilterCategory || state.spatialFilterNameContains),
  );

  const needsThreshold = NEEDS_THRESHOLD.has(state.checkType);
  const needsDirection = NEEDS_DIRECTION.has(state.checkType);
  const needsReference = NEEDS_REFERENCE.has(state.checkType);

  return (
    <section className="card flex flex-col gap-3 p-3">
      <div className="grid grid-cols-2 gap-3">
        <label className="flex flex-col gap-1">
          <span className="text-caption">{strings.geometry.idLabel}</span>
          <Input value={state.id} onChange={(e) => onChange({ id: e.target.value })} placeholder="ducts.parking.floor_clearance" />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-caption">{strings.geometry.categoryLabel}</span>
          <Select value={state.category || undefined} onValueChange={(v) => onChange({ category: v })}>
            <SelectTrigger><SelectValue placeholder={strings.geometry.categoryLabel} /></SelectTrigger>
            <SelectContent>
              {categories.map((c) => (
                <SelectItem key={c.key} value={c.label}>{c.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
      </div>

      <label className="flex flex-col gap-1">
        <span className="text-caption">{strings.geometry.descriptionLabel}</span>
        <Textarea rows={2} value={state.description} onChange={(e) => onChange({ description: e.target.value })} />
      </label>

      <div className="grid grid-cols-3 gap-3">
        <label className="flex flex-col gap-1">
          <span className="text-caption">{strings.geometry.checkTypeLabel}</span>
          <Select
            value={state.checkType}
            onValueChange={(v) => onChange({ checkType: v as GeometryFormState["checkType"] })}
          >
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              {CHECK_TYPES.map((t) => (
                <SelectItem key={t} value={t}>
                  {strings.geometry.checkTypeOptions[t]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
        {needsThreshold && (
          <label className="flex flex-col gap-1">
            <span className="text-caption">{strings.geometry.thresholdMm}</span>
            <Input
              inputMode="decimal"
              value={state.thresholdMm}
              onChange={(e) => onChange({ thresholdMm: e.target.value })}
            />
          </label>
        )}
        {needsDirection && (
          <label className="flex flex-col gap-1">
            <span className="text-caption">{strings.geometry.direction}</span>
            <Select
              value={state.direction}
              onValueChange={(v) => onChange({ direction: v as GeometryFormState["direction"] })}
            >
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {(["below", "above", "horizontal"] as const).map((d) => (
                  <SelectItem key={d} value={d}>
                    {strings.geometry.directionOptions[d]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
        )}
      </div>

      {needsReference && (
        <div className="flex flex-col gap-2 border-t border-[var(--border)] pt-3">
          <span className="text-caption">{strings.geometry.referenceTitle}</span>
          <div className="grid grid-cols-2 gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-caption">{strings.geometry.referenceCategory}</span>
              <Select
                value={state.referenceCategory || undefined}
                onValueChange={(v) => onChange({ referenceCategory: v })}
              >
                <SelectTrigger><SelectValue placeholder={strings.geometry.referenceCategory} /></SelectTrigger>
                <SelectContent>
                  {categories.map((c) => (
                    <SelectItem key={c.key} value={c.label}>{c.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-caption">{strings.geometry.referenceSource}</span>
              <Select
                value={state.referenceSource}
                onValueChange={(v) =>
                  onChange({ referenceSource: v as GeometryFormState["referenceSource"] })
                }
              >
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {(["same_model", "linked_arch", "linked_struct", "linked_mep"] as const).map(
                    (s) => (
                      <SelectItem key={s} value={s}>
                        {strings.geometry.referenceSourceOptions[s]}
                      </SelectItem>
                    ),
                  )}
                </SelectContent>
              </Select>
            </label>
          </div>
          {state.referenceSource !== "same_model" && (
            <label className="flex flex-col gap-1">
              <span className="text-caption">{strings.geometry.referenceLinkHint}</span>
              <Input
                value={state.referenceLinkHint}
                onChange={(e) => onChange({ referenceLinkHint: e.target.value })}
                placeholder={strings.geometry.referenceLinkHintPlaceholder}
              />
            </label>
          )}
        </div>
      )}

      <Collapsible open={filterOpen} onOpenChange={setFilterOpen}>
        <CollapsibleTrigger asChild>
          <Button variant="ghost" size="sm" className="w-fit">
            {strings.geometry.spatialFilterToggle}
          </Button>
        </CollapsibleTrigger>
        <CollapsibleContent className="grid grid-cols-2 gap-3 pt-2">
          <label className="flex flex-col gap-1">
            <span className="text-caption">{strings.geometry.spatialFilterCategory}</span>
            <Select
              value={state.spatialFilterCategory || undefined}
              onValueChange={(v) => onChange({ spatialFilterCategory: v })}
            >
              <SelectTrigger><SelectValue placeholder={strings.geometry.spatialFilterCategory} /></SelectTrigger>
              <SelectContent>
                {categories.map((c) => (
                  <SelectItem key={c.key} value={c.label}>{c.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-caption">{strings.geometry.spatialFilterNameContains}</span>
            <Input
              value={state.spatialFilterNameContains}
              onChange={(e) => onChange({ spatialFilterNameContains: e.target.value })}
              placeholder="Parking"
            />
          </label>
        </CollapsibleContent>
      </Collapsible>
    </section>
  );
}
