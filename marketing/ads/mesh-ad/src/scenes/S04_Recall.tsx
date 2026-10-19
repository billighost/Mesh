import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts } from "../lib/tokens";
import TerminalPanel from "../components/TerminalPanel";
import MemoryCard from "../components/MemoryCard";
import GridField from "../components/GridField";
import Kicker from "../components/Kicker";
import SearchRipple from "../components/SearchRipple";

const recallLines = [
  ">>> agent_b = Mesh(namespace=\"checkout-service\",",
  "...               agent_id=\"frontend-agent\")",
  ">>> agent_b.recall(",
  "...   \"what port does production postgres use?\",",
  "...   n=3",
  "... )",
  "ok  searching / hybrid 5-stage / 0.09 ms",
  "[0.93]  PostgreSQL 16 runs on port 5432",
  "        source: backend-agent  ·  tags: db, postgres, prod",
];

/**
 * Scene 4 — mesh_recall (frames 0–600)
 * Frontend-agent terminal, search ripple, memory card glows lime and data line draws.
 */
export default function S04_Recall() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Existing memory card shown from the start (carried over from scene 3)
  const memCardOpacity = interpolate(frame, [0, 20], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // New terminal panel slides in from right
  const terminalSpring = spring({
    frame: frame - 10,
    fps,
    config: { damping: 22, stiffness: 110, mass: 0.8 },
  });
  const termX = interpolate(terminalSpring, [0, 1], [80, 0]);
  const termOpacity = interpolate(terminalSpring, [0, 1], [0, 1]);

  // Ripple starts after recall fires (~frame 220)
  const rippleStart = 220;

  // Connection line from memory card to terminal (after ripple)
  const lineStart = 280;
  const lineProgress = interpolate(frame, [lineStart, lineStart + 30], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const lineOpacity = interpolate(frame, [lineStart, lineStart + 10], [0, 0.7], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Memory card glows when hit by recall
  const glowOpacity = interpolate(frame, [rippleStart, rippleStart + 20, rippleStart + 60, rippleStart + 90], [0, 0.4, 0.4, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Caption fade in
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

      <div style={{ position: "absolute", top: 64, left: 80, opacity: kickerOpacity }}>
        <Kicker>mesh_recall</Kicker>
      </div>

      {/* SVG layer for ripple + connection line */}
      <svg
        style={{
          position: "absolute",
          inset: 0,
          width: "100%",
          height: "100%",
          pointerEvents: "none",
        }}
      >
        {/* Search ripple from memory card center (~left side) */}
        <SearchRipple
          cx={480}
          cy={540}
          startFrame={rippleStart}
          color={colors.accent}
          maxRadius={220}
          rings={4}
        />

        {/* Data line from card to terminal */}
        <line
          x1={700}
          y1={540}
          x2={1100}
          y2={400}
          stroke={colors.accent}
          strokeWidth={1.5}
          strokeDasharray={`${520 * lineProgress} 9999`}
          opacity={lineOpacity}
          strokeLinecap="round"
        />
        {/* Packet dot */}
        {lineProgress > 0.1 && lineProgress < 0.95 && (
          <circle
            cx={700 + (1100 - 700) * lineProgress}
            cy={540 + (400 - 540) * lineProgress}
            r={4}
            fill={colors.accent}
            opacity={0.9}
          />
        )}
      </svg>

      {/* Two column layout */}
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
        {/* Left: existing memory card */}
        <div
          style={{
            flex: "0 0 auto",
            opacity: memCardOpacity,
            position: "relative",
          }}
        >
          {/* Lime glow when hit by recall */}
          <div
            aria-hidden
            style={{
              position: "absolute",
              inset: -20,
              borderRadius: 8,
              background: `radial-gradient(ellipse, ${colors.accent} 0%, transparent 70%)`,
              opacity: glowOpacity,
              pointerEvents: "none",
            }}
          />
          <MemoryCard
            content="PostgreSQL 16 runs on port 5432"
            memoryType="fact"
            source="backend-agent"
            tags={[
              { label: "db", variant: "accent" },
              { label: "postgres", variant: "accent" },
              { label: "prod" },
            ]}
            entranceFrame={0}
            slideFrom="none"
            width={620}
          />
        </div>

        {/* Right: frontend-agent terminal */}
        <div
          style={{
            flex: "0 0 auto",
            opacity: termOpacity,
            transform: `translateX(${termX}px)`,
          }}
        >
          <TerminalPanel
            title="frontend-agent / checkout-service"
            lines={recallLines}
            startFrame={50}
            width={840}
            speed={0.75}
          />
        </div>
      </AbsoluteFill>

      {/* Caption */}
      <div
        style={{
          position: "absolute",
          bottom: 70,
          left: 0,
          right: 0,
          textAlign: "center",
          opacity: captionOpacity,
        }}
      >
        <span
          style={{
            fontFamily: fonts.display,
            fontWeight: 700,
            fontSize: 38,
            letterSpacing: "-0.035em",
            color: colors.ink,
          }}
        >
          Recall by meaning.{" "}
          <span style={{ color: colors.muted }}>Not by keyword.</span>
        </span>
      </div>
    </AbsoluteFill>
  );
}
