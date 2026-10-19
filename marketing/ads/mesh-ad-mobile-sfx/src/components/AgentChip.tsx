import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts, radius } from "../lib/tokens";

interface AgentChipProps {
  name: string;
  active?: boolean;
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

  const blink = (Math.sin(frame * 0.08) + 1) / 2;
  const dotOpacity = active ? 0.65 + blink * 0.35 : 0.45;

  return (
    <div
      style={{
        opacity,
        transform: `scale(${scale})`,
        display: "inline-flex",
        alignItems: "center",
        gap: 12,
        padding: "16px 24px",
        background: active ? colors.surface2 : colors.surface,
        border: `2px solid ${active ? colors.accent : colors.line}`,
        borderRadius: radius.md,
        boxShadow: active
          ? `0 0 24px rgba(200, 240, 74, 0.22), 5px 5px 0px #000`
          : "5px 5px 0px #000",
        fontFamily: fonts.mono,
        fontSize: 22,
        fontWeight: 700,
        color: active ? colors.accent : colors.ink,
        letterSpacing: "0.02em",
        whiteSpace: "nowrap",
        ...style,
      }}
    >
      <span
        style={{
          width: 12,
          height: 12,
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