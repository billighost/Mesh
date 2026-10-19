import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts } from "../lib/tokens";
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
  "ok  indexed / dedupe on / 0.08 ms",
];

/**
 * Scene 3 — mesh_learn (frames 0–600)
 * Code types in a terminal, memory card crystallises on the right.
 */
export default function S03_Learn() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Panel slides in from left
  const leftPanelSpring = spring({
    frame,
    fps,
    config: { damping: 22, stiffness: 110, mass: 0.8 },
  });
  const leftX = interpolate(leftPanelSpring, [0, 1], [-60, 0]);
  const leftOpacity = interpolate(leftPanelSpring, [0, 1], [0, 1]);

  // Kicker
  const kickerOpacity = interpolate(frame, [10, 35], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Memory card crystallises after code finishes (approx frame 340)
  const cardFrame = 340;

  // Small mark pulse when card appears
  const markPulse = interpolate(frame, [cardFrame + 5, cardFrame + 20, cardFrame + 35], [0, 1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const markScale = 1 + markPulse * 0.15;
  const markOpacity = interpolate(frame, [cardFrame - 10, cardFrame + 5], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill style={{ background: colors.bg }}>
      <GridField opacity={0.25} mask="center" />

      {/* Kicker top-left */}
      <div style={{ position: "absolute", top: 64, left: 80, opacity: kickerOpacity }}>
        <Kicker>mesh_learn</Kicker>
      </div>

      {/* Two-column layout */}
      <AbsoluteFill
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 50,
          padding: "0 60px",
          paddingTop: 40,
        }}
      >
        {/* Left: terminal */}
        <div
          style={{
            opacity: leftOpacity,
            transform: `translateX(${leftX}px)`,
            flex: "0 0 auto",
          }}
        >
          <TerminalPanel
            title="mesh / checkout-service"
            lines={codeLines}
            startFrame={30}
            width={840}
            speed={0.7}
          />
        </div>

        {/* Right: memory card + mark */}
        <div
          style={{
            flex: "0 0 auto",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: 28,
          }}
        >
          {/* Mesh mark pulses when memory lands */}
          <div
            style={{
              opacity: markOpacity,
              transform: `scale(${markScale})`,
              display: "flex",
              alignItems: "center",
              gap: 12,
              fontFamily: fonts.mono,
              fontSize: 20,
              fontWeight: 600,
              color: colors.accentText,
              letterSpacing: "0.06em",
            }}
          >
            <svg width="32" height="32" viewBox="0 0 64 64" fill="none"
              stroke={colors.accentText} strokeWidth="4.5" strokeLinecap="round">
              <path d="M27.6 21.1 L16.4 41.9" />
              <path d="M36.4 21.1 L47.6 41.9" />
              <path d="M19.0 50.0 L45.0 50.0" />
              <circle cx="32" cy="13" r="7" />
              <circle cx="12" cy="50" r="7" />
              <circle cx="52" cy="50" r="7" />
            </svg>
            memory stored
          </div>

          <MemoryCard
            content={"PostgreSQL 16 runs on port 5432"}
            memoryType="fact"
            source="backend-agent"
            tags={[
              { label: "db", variant: "accent" },
              { label: "postgres", variant: "accent" },
              { label: "prod" },
            ]}
            entranceFrame={cardFrame}
            slideFrom="bottom"
            width={640}
          />
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
}
