import * as React from "react";
import * as SelectPrimitive from "@radix-ui/react-select";
import { Check, ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

export const Select = SelectPrimitive.Root;
export const SelectValue = SelectPrimitive.Value;

export function SelectTrigger({
  className,
  children,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Trigger>) {
  return (
    <SelectPrimitive.Trigger
      className={cn(
        "flex h-8 w-full items-center justify-between rounded-[var(--radius)] border border-[var(--border)] bg-[var(--surface)] px-2 text-[13px] text-[var(--ink)] data-[placeholder]:text-[var(--ink-muted)]",
        className,
      )}
      {...props}
    >
      {children}
      <SelectPrimitive.Icon>
        <ChevronDown size={14} className="text-[var(--ink-muted)]" />
      </SelectPrimitive.Icon>
    </SelectPrimitive.Trigger>
  );
}

export function SelectContent({
  className,
  children,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Content>) {
  return (
    <SelectPrimitive.Portal>
      <SelectPrimitive.Content
        className={cn("z-50 card p-1 min-w-[8rem]", className)}
        position="popper"
        sideOffset={4}
        {...props}
      >
        <SelectPrimitive.Viewport>{children}</SelectPrimitive.Viewport>
      </SelectPrimitive.Content>
    </SelectPrimitive.Portal>
  );
}

export function SelectItem({
  className,
  children,
  ...props
}: React.ComponentProps<typeof SelectPrimitive.Item>) {
  return (
    <SelectPrimitive.Item
      className={cn(
        "relative flex h-8 cursor-pointer select-none items-center rounded-[calc(var(--radius)-2px)] px-2 pr-6 text-[13px] outline-none data-[highlighted]:bg-[var(--surface-2)]",
        className,
      )}
      {...props}
    >
      <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
      <SelectPrimitive.ItemIndicator className="absolute right-2">
        <Check size={14} />
      </SelectPrimitive.ItemIndicator>
    </SelectPrimitive.Item>
  );
}
