import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import { colors, markNodes, markPaths } from "../lib/tokens";

interface MeshMarkProps {
  size?: number;
  color?: string;
  /** If true, animates the stroke drawing in from frame 0 */
  animate?: boolean;
  /** Frame offset to start the draw animation */
  startFrame?: number;
  style?: React.CSSProperties;
}

// Path lengths (approximate for the 64×64 viewBox geometry)
const PATH_LENGTHS = {
  diagLeft: 27.2,
  diagRight: 27.2,
  baseline: 26.0,
};
const NODE_CIRCUMFERENCES = {
  top: 2 * Math.PI * markNodes.top.r,           // ~44
  bottomLeft: 2 * Math.PI * markNodes.bottomLeft.r,  // ~44
  bottomRight: 2 * Math.PI * markNodes.bottomRight.r, // ~44
};

export default function MeshMark({
  size = 64,
  color = colors.accent,
  animate = false,
  startFrame = 0,
  style,
}: MeshMarkProps) {
  const frame = useCurrentFrame();
  const elapsed = Math.max(0, frame - startFrame);

  // Animation: each element draws in over ~18 frames, staggered
  const getProgress = (delay: number, duration = 18) =>
    animate
      ? interpolate(elapsed, [delay, delay + duration], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        })
      : 1;

  // Draw order: top node → diag-left → diag-right → bottom-left → bottom-right → baseline
  const topProgress = getProgress(0);
  const diagLProgress = getProgress(10);
  const diagRProgress = getProgress(14);
  const blProgress = getProgress(22);
  const brProgress = getProgress(26);
  const baseProgress = getProgress(34);

  const strokeDash = (length: number, progress: number) =>
    `${length * progress} ${length * (1 - progress) + 9999}`;

  const scale = size / 64;

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      style={{ display: "block", ...style }}
      aria-hidden="true"
    >
      <g
        fill="none"
        stroke={color}
        strokeWidth={4.5}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {/* Diagonals */}
        <path
          d={markPaths.diagLeft}
          strokeDasharray={strokeDash(PATH_LENGTHS.diagLeft, diagLProgress)}
          strokeDashoffset={0}
        />
        <path
          d={markPaths.diagRight}
          strokeDasharray={strokeDash(PATH_LENGTHS.diagRight, diagRProgress)}
          strokeDashoffset={0}
        />
        {/* Baseline */}
        <path
          d={markPaths.baseline}
          strokeDasharray={strokeDash(PATH_LENGTHS.baseline, baseProgress)}
          strokeDashoffset={0}
        />
        {/* Top node */}
        <circle
          cx={markNodes.top.cx}
          cy={markNodes.top.cy}
          r={markNodes.top.r}
          strokeDasharray={strokeDash(NODE_CIRCUMFERENCES.top, topProgress)}
          strokeDashoffset={NODE_CIRCUMFERENCES.top * 0.25}
        />
        {/* Bottom-left node */}
        <circle
          cx={markNodes.bottomLeft.cx}
          cy={markNodes.bottomLeft.cy}
          r={markNodes.bottomLeft.r}
          strokeDasharray={strokeDash(NODE_CIRCUMFERENCES.bottomLeft, blProgress)}
          strokeDashoffset={NODE_CIRCUMFERENCES.bottomLeft * 0.25}
        />
        {/* Bottom-right node */}
        <circle
          cx={markNodes.bottomRight.cx}
          cy={markNodes.bottomRight.cy}
          r={markNodes.bottomRight.r}
          strokeDasharray={strokeDash(NODE_CIRCUMFERENCES.bottomRight, brProgress)}
          strokeDashoffset={NODE_CIRCUMFERENCES.bottomRight * 0.25}
        />
      </g>
    </svg>
  );
}
