import * as React from "react"
import { Input as InputPrimitive } from "@base-ui/react/input"

import { cn } from "@/lib/utils"

function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  // Base UI's Input is a FieldControl: a *controlled* input warns (and Base UI
  // resets the field) if its `value` flips to null/undefined mid-lifetime —
  // which happens when a bound value comes from still-loading or sparse data.
  // If a `value` prop is present (controlled intent), coerce a nullish value
  // to "" so the input stays controlled. Inputs that omit `value` and rely on
  // `defaultValue` (uncontrolled) are left untouched.
  if ("value" in props && props.value == null) {
    props = { ...props, value: "" }
  }
  return (
    <InputPrimitive
      type={type}
      data-slot="input"
      className={cn(
        "h-8 w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 py-1 text-base transition-colors outline-none file:inline-flex file:h-6 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:cursor-not-allowed disabled:bg-input/50 disabled:opacity-50 aria-invalid:border-destructive aria-invalid:ring-3 aria-invalid:ring-destructive/20 md:text-sm dark:bg-input/30 dark:disabled:bg-input/80 dark:aria-invalid:border-destructive/50 dark:aria-invalid:ring-destructive/40",
        className
      )}
      {...props}
    />
  )
}

export { Input }
