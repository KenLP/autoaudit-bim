import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

export const Sheet = DialogPrimitive.Root;
export const SheetTrigger = DialogPrimitive.Trigger;

export function SheetContent({
  className,
  children,
  side = "right",
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content> & {
  side?: "right" | "bottom";
}) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/40" />
      <DialogPrimitive.Content
        className={cn(
          "fixed z-50 bg-[var(--surface)] border-[var(--border)] shadow-lg overflow-y-auto",
          side === "right" &&
            "right-0 top-0 h-full w-full max-w-[420px] border-l p-4",
          side === "bottom" &&
            "bottom-0 left-0 right-0 max-h-[85vh] border-t rounded-t-[var(--radius)] p-4",
          className,
        )}
        {...props}
      >
        {children}
        <DialogPrimitive.Close className="absolute right-3 top-3 rounded-[var(--radius)] p-1 text-[var(--ink-muted)] hover:bg-[var(--surface-2)]">
          <X size={16} />
        </DialogPrimitive.Close>
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

export function SheetTitle({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      className={cn("text-section-title mb-3 pr-6", className)}
      {...props}
    />
  );
}
