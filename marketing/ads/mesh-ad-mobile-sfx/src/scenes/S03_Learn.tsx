import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts, MOBILE } from "../lib/tokens";
import TerminalPanel from "../components/TerminalPanel";
import MemoryCard from "../components/MemoryCard";
import GridField from "../components/GridField";
import Kicker from "../components/Kicker";

const codeLines = [
  "$ python",
  ">>> from mesh import Mesh",
  '>>> agent_a = Mesh(namespace="checkout-service",',
  '...               agent_id="backend-agent")',
  '>>> agent_a.learn(',
  '...   "PostgreSQL 16 runs on port 5432",',
  '...   memory_type="fact",',
  '...   tags=["db", "postgres", "prod"],',
  "... )",
  "ok  indexed • dedupe on • 0.08 ms",
];

/**
 * Scene 3 — mesh_learn (frames 0–600)
 * Code types in top terminal, memory card crystallises below at frame 340.
 */
export default function S03_Learn() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const topPanelSpring = spring({
    frame,
    fps,
    config: { damping: 22, stiffness: 110, mass: 0.8 },
  });
  const topY = interpolate(topPanelSpring, [0, 1], [-40, 0]);
  const topOpacity = interpolate(topPanelSpring, [0, 1], [0, 1]);

  const kickerOpacity = interpolate(frame, [10, 35], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Memory card crystallises at frame 340 (synced with crystallize.wav at global 1420)
  const cardFrame = 340;
  const markPulse = interpolate(
    frame,
    [cardFrame + 5, cardFrame + 20, cardFrame + 35],
    [0, 1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );
  const markScale = 1 + markPulse * 0.16;
  const markOpacity = interpolate(frame, [cardFrame - 10, cardFrame + 5], [0, 1], {
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
        <Kicker>mesh_learn</Kicker>
        <span
          style={{
            fontFamily: fonts.mono,
            fontSize: 20,
            fontWeight: 600,
            color: colors.muted,
            letterSpacing: "0.06em",
          }}
        >
          STEP 01 / STORE
        </span>
      </div>

      {/* Vertical stack: Terminal → Connector → MemoryCard */}
      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "flex-start",
          padding: `${MOBILE.safeTop + 82}px ${MOBILE.padX}px ${MOBILE.safeBottom}px`,
          gap: 36,
        }}
      >
        {/* Top: Terminal */}
        <div
          style={{
            opacity: topOpacity,
            transform: `translateY(${topY}px)`,
            width: "100%",
          }}
        >
          <TerminalPanel
            title="backend-agent / checkout-service"
            badge="WRITE"
            lines={codeLines}
            startFrame={30}
            width="100%"
            speed={0.7}
            fontSize={23}
            minHeight={480}
          />
        </div>

        {/* Center status indicator: Mesh mark pulses when memory lands */}
        <div
          style={{
            opacity: markOpacity,
            transform: `scale(${markScale})`,
            display: "flex",
            alignItems: "center",
            gap: 16,
            fontFamily: fonts.mono,
            fontSize: 24,
            fontWeight: 700,
            color: colors.accent,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            background: colors.accentSoft,
            border: `1.5px solid ${colors.accent}`,
            padding: "12px 28px",
            borderRadius: 999,
          }}
        >
          <svg
            width="32"
            height="32"
            viewBox="0 0 64 64"
            fill="none"
            stroke={colors.accent}
            strokeWidth="4.5"
            strokeLinecap="round"
          >
            <path d="M27.6 21.1 L16.4 41.9" />
            <path d="M36.4 21.1 L47.6 41.9" />
            <path d="M19.0 50.0 L45.0 50.0" />
            <circle cx="32" cy="13" r="7" />
            <circle cx="12" cy="50" r="7" />
            <circle cx="52" cy="50" r="7" />
          </svg>
          <span>↓ Memory Crystallized in 0.08 ms</span>
        </div>

        {/* Bottom: MemoryCard */}
        <MemoryCard
          content="PostgreSQL 16 runs on port 5432"
          memoryType="fact"
          source="backend-agent"
          tags={[
            { label: "db", variant: "accent" },
            { label: "postgres", variant: "accent" },
            { label: "prod" },
          ]}
          entranceFrame={cardFrame}
          slideFrom="bottom"
          width="100%"
        />

        {/* Takeaway caption */}
        <div
          style={{
            marginTop: "auto",
            marginBottom: 20,
            opacity: markOpacity,
            textAlign: "center",
            fontFamily: fonts.display,
            fontSize: 38,
            fontWeight: 700,
            letterSpacing: "-0.03em",
            color: colors.ink,
          }}
        >
          One agent learns.{" "}
          <span style={{ color: colors.accent }}>Auto-deduped &amp; indexed.</span>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
}