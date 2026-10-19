import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { colors } from "../lib/tokens";
import MeshMark from "../components/MeshMark";

/**
 * Scene 0 — Cold Open (0s–3s / frames 0–179)
 * Pure darkness. The Mesh 3-node mark draws itself in lime.
 */
export default function S00_ColdOpen() {
  const frame = useCurrentFrame();

  // Fade in the whole scene at the very start
  const bgOpacity = interpolate(frame, [0, 12], [0, 1], {
    extrapolateRight: "clamp",
  });

  // Mark size pulses slightly after drawing
  const markDrawDone = 55; // frames for mark draw to complete
  const pulse = interpolate(
    frame,
    [markDrawDone, markDrawDone + 20, markDrawDone + 40],
    [0, 1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );
  const markScale = 1 + pulse * 0.025;

  // Subtle accent-soft glow expands from center after mark appears
  const glowOpacity = interpolate(frame, [markDrawDone, markDrawDone + 30], [0, 0.15], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const glowRadius = interpolate(frame, [markDrawDone, markDrawDone + 60], [60, 160], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill
      style={{
        background: colors.bg,
        opacity: bgOpacity,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      {/* Glow halo behind mark */}
      <div
        aria-hidden
        style={{
          position: "absolute",
          width: glowRadius * 2,
          height: glowRadius * 2,
          borderRadius: "50%",
          background: `radial-gradient(circle, ${colors.accentSoft} 0%, transparent 70%)`,
          opacity: glowOpacity,
          pointerEvents: "none",
        }}
      />

      {/* The mark itself */}
      <div style={{ transform: `scale(${markScale})` }}>
        <MeshMark
          size={120}
          color={colors.ink}
          animate
          startFrame={10}
        />
      </div>
    </AbsoluteFill>
  );
}
