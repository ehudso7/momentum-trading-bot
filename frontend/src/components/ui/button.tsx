import { cn } from "@/lib/utils";
import { ButtonHTMLAttributes, forwardRef } from "react";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "default" | "outline" | "ghost" | "destructive";
  size?: "sm" | "md" | "lg";
}

const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = "default", size = "md", ...props }, ref) => {
    return (
      <button
        ref={ref}
        className={cn(
          "inline-flex items-center justify-center rounded-md font-medium transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#82988e]/70 disabled:pointer-events-none disabled:opacity-50",
          {
            "border border-[#73877e] bg-[#6f837a] text-[#0f1211] hover:bg-[#80968c]":
              variant === "default",
            "border border-white/12 bg-transparent text-zinc-100 hover:bg-white/[0.05]":
              variant === "outline",
            "text-zinc-300 hover:bg-white/[0.04] hover:text-white":
              variant === "ghost",
            "border border-red-900/60 bg-red-950/40 text-red-200 hover:bg-red-950/60":
              variant === "destructive",
          },
          {
            "h-8 px-3 text-xs": size === "sm",
            "h-10 px-4 text-sm": size === "md",
            "h-12 px-6 text-base": size === "lg",
          },
          className
        )}
        {...props}
      />
    );
  }
);
Button.displayName = "Button";

export { Button };
