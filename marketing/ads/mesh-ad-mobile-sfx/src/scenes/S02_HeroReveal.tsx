import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts, MOBILE, radius } from "../lib/tokens";
import MeshLogo from "../components/MeshLogo";
import GridField from "../components/GridField";
import Kicker from "../components/Kicker";

const specs = ["MIT Licensed", "Local-First", "Python 3.10+", "No External DB"];
const ecosystems = [
  "Claude Code",
  "Cursor",
  "Gemini CLI",
  "Windsurf",
  "Codex",
  "OpenCode",
  "Antigravity",
  ".agents",
];

/**
 * Scene 2 — Hero Reveal (frames 0–360 within sequence)
 * Lime flash → mark draws in large → wordmark → tagline → supported agent ecosystems
 */
export default function S02_HeroReveal() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Initial lime flash (synced with impact_boom.wav at global frame 722)
  const flashOpacity = interpolate(frame, [0, 4, 18, 30], [0, 0.72, 0.3, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const gridOpacity = interpolate(frame, [20, 50], [0, 0.38], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const logoSpring = spring({
    frame: frame - 8,
    fps,
    config: { damping: 20, stiffness: 100, mass: 0.9 },
  });
  const logoScale = interpolate(logoSpring, [0, 1], [0.72, 1]);
  const logoOpacity = interpolate(logoSpring, [0, 1], [0, 1]);

  const kickerOpacity = interpolate(frame, [45, 70], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const kickerY = interpolate(frame, [45, 70], [16, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const taglineWords = ["High-speed", "shared", "semantic", "memory", "for", "AI", "agents."];
  const wordProgress = interpolate(frame, [80, 195], [0, taglineWords.length], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const metaOpacity = interpolate(frame, [195, 235], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const ecoOpacity = interpolate(frame, [225, 265], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const blobOpacity = interpolate(frame, [30, 70], [0, 0.6], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill style={{ background: colors.bg }}>
      {/* Background radial blobs */}
      <div
        aria-hidden
        style={{
          position: "absolute",
          inset: 0,
          opacity: blobOpacity,
          background: `
            radial-gradient(circle at 80% 15%, ${colors.accentSoft}, transparent 35%),
            radial-gradient(circle at 15% 70%, ${colors.surface2}, transparent 35%)
          `,
        }}
      />

      <GridField opacity={gridOpacity} mask="center" />

      {/* Flash overlay */}
      <AbsoluteFill
        style={{
          background: colors.accent,
          opacity: flashOpacity,
          pointerEvents: "none",
        }}
      />

      {/* Central vertical stack */}
      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          padding: `0 ${MOBILE.padX}px`,
          paddingBottom: 90,
          gap: 48,
        }}
      >
        {/* Kicker */}
        <div
          style={{
            opacity: kickerOpacity,
            transform: `translateY(${kickerY}px)`,
          }}
        >
          <Kicker>Shared Memory For AI Agents</Kicker>
        </div>

        {/* Logo */}
        <div
          style={{
            opacity: logoOpacity,
            transform: `scale(${logoScale})`,
            margin: "12px 0",
          }}
        >
          <MeshLogo size={175} startFrame={8} />
        </div>

        {/* Tagline */}
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            justifyContent: "center",
            gap: 16,
            textAlign: "center",
            lineHeight: 1.2,
          }}
        >
          {taglineWords.map((word, i) => {
            const visible = wordProgress > i;
            const isHighlight = word === "semantic" || word === "memory";
            return (
              <span
                key={i}
                style={{
                  fontFamily: fonts.body,
                  fontSize: 56,
                  fontWeight: 700,
                  color: isHighlight ? colors.accent : colors.ink,
                  opacity: visible ? 1 : 0,
                  transform: `translateY(${visible ? 0 : 12}px)`,
                  letterSpacing: "-0.025em",
                }}
              >
                {word}
              </span>
            );
          })}
        </div>

        {/* 2x2 Spec Badges */}
        <div
          style={{
            opacity: metaOpacity,
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: 16,
            width: "100%",
          }}
        >
          {specs.map((item, i) => (
            <div
              key={i}
              style={{
                background: colors.surface,
                border: `1.5px solid ${colors.line}`,
                borderRadius: radius.sm,
                padding: "16px 20px",
                fontFamily: fonts.mono,
                fontSize: 22,
                fontWeight: 600,
                color: colors.ink,
                textAlign: "center",
                letterSpacing: "0.04em",
                boxShadow: "4px 4px 0px #000",
              }}
            >
              <span style={{ color: colors.accent, marginRight: 10 }}>✓</span>
              {item}
            </div>
          ))}
        </div>

        {/* Supported AI Agent Ecosystems (Conversion Proof) */}
        <div
          style={{
            opacity: ecoOpacity,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            gap: 16,
            marginTop: 8,
          }}
        >
          <div
            style={{
              fontFamily: fonts.mono,
              fontSize: 18,
              fontWeight: 600,
              color: colors.muted,
              letterSpacing: "0.12em",
              textTransform: "uppercase",
            }}
          >
            Works seamlessly across every agent
          </div>
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              justifyContent: "center",
              gap: 12,
            }}
          >
            {ecosystems.map((agent) => (
              <span
                key={agent}
                style={{
                  fontFamily: fonts.mono,
                  fontSize: 19,
                  fontWeight: 600,
                  color: colors.muted,
                  background: colors.surface2,
                  border: `1px solid ${colors.line}`,
                  padding: "8px 16px",
                  borderRadius: radius.sm,
                }}
              >
                {agent}
              </span>
            ))}
          </div>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
}