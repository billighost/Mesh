import React from "react";
import { colors } from "../lib/tokens";

interface GridFieldProps {
  opacity?: number;
  mask?: "top" | "center" | "full";
}

export default function GridField({ opacity = 0.35, mask = "top" }: GridFieldProps) {
  const maskMap = {
    top: "radial-gradient(ellipse at 50% 25%, #000 35%, transparent 80%)",
    center: "radial-gradient(ellipse at 50% 50%, #000 45%, transparent 85%)",
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
          backgroundSize: "40px 40px",
          opacity,
          WebkitMaskImage: maskMap[mask],
          maskImage: maskMap[mask],
        }}
      />
    </div>
  );
}