import { cn } from "@/lib/utils";
import { HTMLAttributes } from "react";

interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: "default" | "success" | "warning" | "danger" | "neutral";
}

export function Badge({
  className,
  variant = "default",
  ...props
}: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium",
        {
          "bg-cyan-500/20 text-cyan-300": variant === "default",
          "bg-emerald-500/20 text-emerald-300": variant === "success",
          "bg-amber-500/20 text-amber-300": variant === "warning",
          "bg-red-500/20 text-red-300": variant === "danger",
          "bg-zinc-500/20 text-zinc-300": variant === "neutral",
        },
        className
      )}
      {...props}
    />
  );
}