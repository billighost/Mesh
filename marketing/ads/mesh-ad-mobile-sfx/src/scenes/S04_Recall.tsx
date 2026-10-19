import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts, MOBILE } from "../lib/tokens";
import TerminalPanel from "../components/TerminalPanel";
import MemoryCard from "../components/MemoryCard";
import GridField from "../components/GridField";
import Kicker from "../components/Kicker";
import SearchRipple from "../components/SearchRipple";

const recallLines = [
  '>>> agent_b = Mesh(namespace="checkout-service",',
  '...               agent_id="frontend-agent")',
  ">>> agent_b.recall(",
  '...   "what port does production postgres use?",',
  "...   n=3",
  "... )",
  "ok  searching • hybrid 5-stage • 0.09 ms",
  "[0.93]  PostgreSQL 16 runs on port 5432",
  "        source: backend-agent • tags: db, postgres, prod",
];

/**
 * Scene 4 — mesh_recall (frames 0–600)
 * Stored memory card at top, frontend-agent terminal below, vertical sonar ripple & data packet.
 */
export default function S04_Recall() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const memCardOpacity = interpolate(frame, [0, 20], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const terminalSpring = spring({
    frame: frame - 10,
    fps,
    config: { damping: 22, stiffness: 110, mass: 0.8 },
  });
  const termY = interpolate(terminalSpring, [0, 1], [50, 0]);
  const termOpacity = interpolate(terminalSpring, [0, 1], [0, 1]);

  // Ripple starts at frame 220 (synced with sonar_pulse.wav at global 1900)
  const rippleStart = 220;

  // Vertical connection line from memory card (y=500) to terminal (y=620) at frame 280
  const lineStart = 280;
  const lineProgress = interpolate(frame, [lineStart, lineStart + 30], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const lineOpacity = interpolate(frame, [lineStart, lineStart + 10], [0, 0.85], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Memory card glows lime when hit by recall
  const glowOpacity = interpolate(
    frame,
    [rippleStart, rippleStart + 20, rippleStart + 65, rippleStart + 95],
    [0, 0.45, 0.45, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

  const captionOpacity = interpolate(frame, [380, 420], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const kickerOpacity = interpolate(frame, [10, 35], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill style={{ background: colors.bg }}>
      <GridField opacity={0.25} mask="center" />

      {/* Header bar */}
      <div
        style={{
          position: "absolute",
          top: MOBILE.safeTop,
          left: MOBILE.padX,
          right: MOBILE.padX,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          opacity: kickerOpacity,
        }}
      >
        <Kicker>mesh_recall</Kicker>
        <span
          style={{
            fontFamily: fonts.mono,
            fontSize: 20,
            fontWeight: 600,
            color: colors.accent,
            letterSpacing: "0.06em",
          }}
        >
          0.93 SIMILARITY • 0.09 ms
        </span>
      </div>

      {/* SVG layer for sonar ripple + vertical data transfer line */}
      <svg
        style={{
          position: "absolute",
          inset: 0,
          width: "100%",
          height: "100%",
          pointerEvents: "none",
        }}
      >
        <SearchRipple
          cx={MOBILE.centerX}
          cy={380}
          startFrame={rippleStart}
          color={colors.accent}
          maxRadius={340}
          rings={4}
        />

        <line
          x1={MOBILE.centerX}
          y1={510}
          x2={MOBILE.centerX}
          y2={640}
          stroke={colors.accent}
          strokeWidth={4}
          strokeDasharray={`${130 * lineProgress} 9999`}
          opacity={lineOpacity}
          strokeLinecap="round"
        />

        {lineProgress > 0.05 && lineProgress < 0.98 && (
          <circle
            cx={MOBILE.centerX}
            cy={510 + (640 - 510) * lineProgress}
            r={9}
            fill={colors.accent}
            opacity={0.95}
          />
        )}
      </svg>

      {/* Vertical Stack: MemoryCard (Top) → Terminal (Bottom) */}
      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "flex-start",
          padding: `${MOBILE.safeTop + 82}px ${MOBILE.padX}px ${MOBILE.safeBottom}px`,
          gap: 110,
        }}
      >
        {/* Top: Existing memory card */}
        <div
          style={{
            width: "100%",
            opacity: memCardOpacity,
            position: "relative",
          }}
        >
          <div
            aria-hidden
            style={{
              position: "absolute",
              inset: -24,
              borderRadius: 12,
              background: `radial-gradient(ellipse, ${colors.accent} 0%, transparent 70%)`,
              opacity: glowOpacity,
              pointerEvents: "none",
            }}
          />
          <MemoryCard
            content="PostgreSQL 16 runs on port 5432"
            memoryType="fact"
            source="backend-agent"
            similarity={frame >= 285 ? 0.93 : undefined}
            tags={[
              { label: "db", variant: "accent" },
              { label: "postgres", variant: "accent" },
              { label: "prod" },
            ]}
            resolved={frame >= 280}
            entranceFrame={0}
            slideFrom="none"
            width="100%"
          />
        </div>

        {/* Bottom: frontend-agent terminal */}
        <div
          style={{
            width: "100%",
            opacity: termOpacity,
            transform: `translateY(${termY}px)`,
          }}
        >
          <TerminalPanel
            title="frontend-agent / checkout-service"
            badge="RECALL"
            lines={recallLines}
            startFrame={50}
            width="100%"
            speed={0.75}
            fontSize={22}
            minHeight={440}
          />
        </div>
      </AbsoluteFill>

      {/* Caption above HUD dock */}
      <div
        style={{
          position: "absolute",
          bottom: 220,
          left: MOBILE.padX,
          right: MOBILE.padX,
          textAlign: "center",
          opacity: captionOpacity,
        }}
      >
        <div
          style={{
            fontFamily: fonts.display,
            fontWeight: 800,
            fontSize: 48,
            letterSpacing: "-0.035em",
            color: colors.ink,
            lineHeight: 1.15,
          }}
        >
          Recall by meaning.{" "}
          <span style={{ color: colors.accent }}>Not just keyword.</span>
        </div>
      </div>
    </AbsoluteFill>
  );
}