import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { colors, fonts, MOBILE, radius } from "../lib/tokens";
import MeshMark from "./MeshMark";

interface MobileHUDProps {
  totalFrames: number;
}

export default function MobileHUD({ totalFrames }: MobileHUDProps) {
  const frame = useCurrentFrame();
  const progress = Math.min(1, Math.max(0, frame / totalFrames));

  // Show persistent bottom conversion dock from HeroReveal (frame 780) until Install scene (frame 4790)
  const dockOpacity = interpolate(
    frame,
    [760, 795, 4760, 4795],
    [0, 1, 1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      {/* Top timeline progress bar */}
      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          right: 0,
          height: 8,
          background: colors.surface2,
        }}
      >
        <div
          style={{
            height: "100%",
            width: `${progress * 100}%`,
            background: colors.accent,
            boxShadow: `0 0 16px ${colors.accent}`,
          }}
        />
      </div>

      {/* Persistent Bottom Install Conversion Pill */}
      <div
        style={{
          position: "absolute",
          bottom: 52,
          left: MOBILE.padX,
          right: MOBILE.padX,
          opacity: dockOpacity,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "18px 26px",
          background: "rgba(17, 20, 12, 0.92)",
          border: `1.5px solid ${colors.line}`,
          borderRadius: radius.md,
          boxShadow: "0 12px 32px rgba(0,0,0,0.65)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <MeshMark size={34} color={colors.accent} />
          <div style={{ fontFamily: fonts.mono, fontSize: 22, color: colors.ink, fontWeight: 600 }}>
            <span style={{ color: colors.accent }}>$ </span>
            pip install mesh-context-layer
          </div>
        </div>

        <div
          style={{
            fontFamily: fonts.mono,
            fontSize: 16,
            fontWeight: 700,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            color: colors.accentInk,
            background: colors.accent,
            padding: "6px 14px",
            borderRadius: radius.sm,
          }}
        >
          v0.7.1 • MIT
        </div>
      </div>
    </AbsoluteFill>
  );
}