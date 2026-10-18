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
        gap: 14,
        fontFamily: fonts.mono,
        fontSize: 22,
        fontWeight: 600,
        textTransform: "uppercase",
        letterSpacing: "0.12em",
        color: colors.muted,
        opacity,
        ...style,
      }}
    >
      {/* The lime accent dash */}
      <span
        style={{
          width: 28,
          height: 3,
          background: colors.accentText,
          display: "block",
          flexShrink: 0,
        }}
      />
      {children}
    </div>
  );
}
