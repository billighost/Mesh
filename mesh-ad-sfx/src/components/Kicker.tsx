import React from "react";
import { colors, fonts } from "../lib/tokens";

interface KickerProps {
  children: React.ReactNode;
  opacity?: number;
  style?: React.CSSProperties;
}

/**
 * Small mono uppercase kicker label with a lime accent dash before it.
 * Matches the `.kicker` class from the Mesh landing site.
 */
export default function Kicker({ children, opacity = 1, style }: KickerProps) {
  return (
    <div
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 10,
        fontFamily: fonts.mono,
        fontSize: 13,
        fontWeight: 500,
        textTransform: "uppercase",
        letterSpacing: "0.13em",
        color: colors.muted,
        opacity,
        ...style,
      }}
    >
      {/* The lime accent dash */}
      <span
        style={{
          width: 18,
          height: 1,
          background: colors.accent,
          display: "block",
          flexShrink: 0,
        }}
      />
      {children}
    </div>
  );
}
