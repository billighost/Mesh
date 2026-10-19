import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import { colors } from "../lib/tokens";

interface SearchRippleProps {
  cx: number;
  cy: number;
  startFrame?: number;
  color?: string;
  maxRadius?: number;
  rings?: number;
}

export default function SearchRipple({
  cx,
  cy,
  startFrame = 0,
  color = colors.accent,
  maxRadius = 280,
  rings = 4,
}: SearchRippleProps) {
  const frame = useCurrentFrame();
  const elapsed = Math.max(0, frame - startFrame);
  const duration = 60;

  return (
    <g>
      {Array.from({ length: rings }, (_, i) => {
        const ringElapsed = Math.max(0, elapsed - i * 12);
        const t = Math.min(1, ringElapsed / duration);
        const r = interpolate(t, [0, 1], [0, maxRadius]);
        const opacity = interpolate(t, [0, 0.2, 0.8, 1], [0, 0.65, 0.25, 0]);
        return (
          <circle
            key={i}
            cx={cx}
            cy={cy}
            r={r}
            fill="none"
            stroke={color}
            strokeWidth={2.5}
            opacity={opacity}
          />
        );
      })}
    </g>
  );
}