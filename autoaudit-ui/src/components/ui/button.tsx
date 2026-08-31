import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-[var(--radius)] text-[13px] font-medium transition-colors disabled:pointer-events-none disabled:opacity-50 h-8 px-3",
  {
    variants: {
      variant: {
        primary:
          "bg-[var(--primary)] text-[var(--primary-ink)] hover:bg-[var(--primary-hover)]",
        outline:
          "border border-[var(--border)] bg-[var(--surface)] text-[var(--ink)] hover:bg-[var(--surface-2)]",
        ghost: "text-[var(--ink)] hover:bg-[var(--surface-2)]",
        destructive: "bg-[var(--fail)] text-white hover:opacity-90",
        link: "text-[var(--primary)] underline-offset-4 hover:underline h-auto p-0",
      },
      size: {
        default: "h-8 px-3",
        sm: "h-7 px-2 text-[12px]",
        icon: "h-8 w-8 p-0",
      },
    },
    defaultVariants: { variant: "primary", size: "default" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => (
    <button
      ref={ref}
      className={cn(buttonVariants({ variant, size }), className)}
      {...props}
    />
  ),
);
Button.displayName = "Button";
