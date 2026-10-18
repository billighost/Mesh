import React from "react";
import { colors } from "../lib/tokens";

interface GridFieldProps {
  opacity?: number;
  /** Radial mask focus: "top" | "center" | "full" */
  mask?: "top" | "center" | "full";
}

/**
 * The 32×32 grid-field texture from the Mesh landing site.
 * Faint hairlines at --line color, radial-masked.
 */
export default function GridField({ opacity = 0.35, mask = "top" }: GridFieldProps) {
  const maskMap = {
    top: "radial-gradient(ellipse at 50% 30%, #000 30%, transparent 75%)",
    center: "radial-gradient(ellipse at 50% 50%, #000 40%, transparent 80%)",
    full: "none",
  };

  return (
    <div
      aria-hidden="true"
      style={{
        position: "absolute",
        inset: 0,
        pointerEvents: "none",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          position: "absolute",
          inset: 0,
          backgroundImage: `
            linear-gradient(${colors.line} 1px, transparent 1px),
            linear-gradient(90deg, ${colors.line} 1px, transparent 1px)
          `,
          backgroundSize: "32px 32px",
          opacity,
          WebkitMaskImage: maskMap[mask],
          maskImage: maskMap[mask],
        }}
      />
    </div>
  );
}
