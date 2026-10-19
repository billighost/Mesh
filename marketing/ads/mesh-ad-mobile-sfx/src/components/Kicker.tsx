import React from "react";
import { colors, fonts } from "../lib/tokens";

interface KickerProps {
  children: React.ReactNode;
  opacity?: number;
  style?: React.CSSProperties;
}

export default function Kicker({ children, opacity = 1, style }: KickerProps) {
  return (
    <div
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 16,
        fontFamily: fonts.mono,
        fontSize: 25,
        fontWeight: 700,
        textTransform: "uppercase",
        letterSpacing: "0.12em",
        color: colors.accent,
        opacity,
        ...style,
      }}
    >
      <span
        style={{
          width: 34,
          height: 4,
          background: colors.accent,
          display: "block",
          flexShrink: 0,
        }}
      />
      <span style={{ color: colors.ink }}>{children}</span>
    </div>
  );
}