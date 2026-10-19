import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts, radius } from "../lib/tokens";

interface AgentChipProps {
  name: string;
  active?: boolean;
  /** Frame for spring entrance */
  entranceFrame?: number;
  style?: React.CSSProperties;
}

export default function AgentChip({
  name,
  active = false,
  entranceFrame = 0,
  style,
}: AgentChipProps) {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const s = spring({
    frame: frame - entranceFrame,
    fps,
    config: { damping: 20, stiffness: 140, mass: 0.5 },
  });
  const opacity = interpolate(s, [0, 1], [0, 1]);
  const scale = interpolate(s, [0, 1], [0.88, 1]);

  // Blinking presence dot
  const blink = (Math.sin(frame * 0.08) + 1) / 2;
  const dotOpacity = active ? 0.6 + blink * 0.4 : 0.4;

  return (
    <div
      style={{
        opacity,
        transform: `scale(${scale})`,
        display: "inline-flex",
        alignItems: "center",
        gap: 10,
        padding: "12px 20px",
        background: colors.surface,
        border: `1px solid ${active ? colors.accent : colors.line}`,
        borderRadius: radius.sm,
        boxShadow: active ? `3px 3px 0px ${colors.accentInk}` : "3px 3px 0px #000",
        fontFamily: fonts.mono,
        fontSize: 18,
        fontWeight: 600,
        color: active ? colors.accentText : colors.ink,
        letterSpacing: "0.03em",
        whiteSpace: "nowrap",
        ...style,
      }}
    >
      {/* Presence dot */}
      <span
        style={{
          width: 9,
          height: 9,
          borderRadius: "50%",
          background: active ? colors.accent : colors.muted,
          opacity: dotOpacity,
          display: "inline-block",
          flexShrink: 0,
        }}
      />
      {name}
    </div>
  );
}
