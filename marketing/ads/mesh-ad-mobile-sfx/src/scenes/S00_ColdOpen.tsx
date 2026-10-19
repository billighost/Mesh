import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts, MOBILE, radius } from "../lib/tokens";
import MeshMark from "../components/MeshMark";
import GridField from "../components/GridField";

/**
 * Scene 0 — Cold Open (0s–3s / frames 0–179)
 * Pure darkness. The Mesh 3-node mark draws itself in lime with a scroll-stopping mobile hook.
 */
export default function S00_ColdOpen() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const bgOpacity = interpolate(frame, [0, 12], [0, 1], {
    extrapolateRight: "clamp",
  });

  // Mark draw finishes at frame 55 (synced with 3 node pings at 10, 24, 38)
  const markDrawDone = 55;
  const pulse = interpolate(
    frame,
    [markDrawDone, markDrawDone + 20, markDrawDone + 40],
    [0, 1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );
  const markScale = 1 + pulse * 0.05;

  // Radial glow expands behind the mark
  const glowOpacity = interpolate(frame, [markDrawDone, markDrawDone + 30], [0, 0.28], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const glowRadius = interpolate(frame, [markDrawDone, markDrawDone + 60], [120, 320], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Scroll-stopper text pill springs in right as the mark completes
  const hookSpring = spring({
    frame: frame - markDrawDone,
    fps,
    config: { damping: 20, stiffness: 130, mass: 0.6 },
  });
  const hookOpacity = interpolate(hookSpring, [0, 1], [0, 1]);
  const hookY = interpolate(hookSpring, [0, 1], [24, 0]);

  return (
    <AbsoluteFill
      style={{
        background: colors.bg,
        opacity: bgOpacity,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <GridField opacity={0.22} mask="center" />

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
      <div style={{ transform: `scale(${markScale})`, marginBottom: 56 }}>
        <MeshMark size={210} color={colors.accent} animate startFrame={10} />
      </div>

      {/* Scroll-stopper hook badge */}
      <div
        style={{
          opacity: hookOpacity,
          transform: `translateY(${hookY}px)`,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 18,
          padding: `0 ${MOBILE.padX}px`,
          textAlign: "center",
        }}
      >
        <div
          style={{
            fontFamily: fonts.mono,
            fontSize: 22,
            fontWeight: 700,
            letterSpacing: "0.14em",
            textTransform: "uppercase",
            color: colors.accent,
            background: colors.accentSoft,
            border: `1.5px solid ${colors.accent}`,
            padding: "10px 24px",
            borderRadius: radius.sm,
          }}
        >
          ⚡ Shared Agent Memory Layer
        </div>

        <div
          style={{
            fontFamily: fonts.display,
            fontSize: 40,
            fontWeight: 700,
            letterSpacing: "-0.035em",
            color: colors.ink,
          }}
        >
          Stop repeating yourself to AI.
        </div>
      </div>
    </AbsoluteFill>
  );
}