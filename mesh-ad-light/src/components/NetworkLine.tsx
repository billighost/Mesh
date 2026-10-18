import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import { colors } from "../lib/tokens";

interface NetworkLineProps {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  /** Frame to start drawing */
  startFrame?: number;
  duration?: number;
  color?: string;
  strokeWidth?: number;
  /** Whether to animate a "data packet" dot along the line */
  showPacket?: boolean;
  packetDelay?: number;
}

export default function NetworkLine({
  x1, y1, x2, y2,
  startFrame = 0,
  duration = 20,
  color = colors.accentText,
  strokeWidth = 2,
  showPacket = false,
  packetDelay = 0,
}: NetworkLineProps) {
  const frame = useCurrentFrame();
  const elapsed = Math.max(0, frame - startFrame);

  const lineLength = Math.hypot(x2 - x1, y2 - y1);
  const progress = interpolate(elapsed, [0, duration], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const lineOpacity = interpolate(elapsed, [0, 6], [0, 0.7], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Packet animation
  const packetElapsed = Math.max(0, elapsed - duration - packetDelay);
  const packetT = interpolate(packetElapsed, [0, 24], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const packetOpacity = interpolate(
    packetElapsed,
    [0, 4, 20, 24],
    [0, 1, 1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );
  const px = x1 + (x2 - x1) * packetT;
  const py = y1 + (y2 - y1) * packetT;

  return (
    <g>
      <line
        x1={x1}
        y1={y1}
        x2={x2}
        y2={y2}
        stroke={color}
        strokeWidth={strokeWidth}
        opacity={lineOpacity}
        strokeDasharray={`${lineLength * progress} ${lineLength * (1 - progress) + 9999}`}
        strokeLinecap="round"
      />
      {showPacket && (
        <circle
          cx={px}
          cy={py}
          r={4}
          fill={color}
          opacity={packetOpacity}
        />
      )}
    </g>
  );
}
