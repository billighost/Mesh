import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts } from "../lib/tokens";
import MeshLogo from "../components/MeshLogo";
import GridField from "../components/GridField";
import Kicker from "../components/Kicker";

/**
 * Scene 2 — Hero Reveal (frames 0–360 within this sequence)
 * Lime flash → mark draws in large → wordmark → tagline
 */
export default function S02_HeroReveal() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Initial lime flash
  const flashOpacity = interpolate(frame, [0, 4, 18, 30], [0, 0.7, 0.3, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Grid fades in
  const gridOpacity = interpolate(frame, [20, 50], [0, 0.35], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Logo entrance spring
  const logoSpring = spring({
    frame: frame - 8,
    fps,
    config: { damping: 20, stiffness: 100, mass: 0.9 },
  });
  const logoScale = interpolate(logoSpring, [0, 1], [0.7, 1]);
  const logoOpacity = interpolate(logoSpring, [0, 1], [0, 1]);

  // Kicker fades in
  const kickerOpacity = interpolate(frame, [45, 70], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const kickerY = interpolate(frame, [45, 70], [12, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Tagline word-by-word
  const taglineWords = ["High-speed", "shared", "semantic", "memory", "for", "AI", "agents."];
  const wordProgress = interpolate(frame, [80, 200], [0, taglineWords.length], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Background glow blobs (matching landing site's radial gradients)
  const blobOpacity = interpolate(frame, [30, 70], [0, 0.55], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill style={{ background: colors.bg }}>
      {/* Background blobs */}
      <div
        aria-hidden
        style={{
          position: "absolute",
          inset: 0,
          opacity: blobOpacity,
          background: `
            radial-gradient(circle at 82% -6%, ${colors.accentSoft}, transparent 28%),
            radial-gradient(circle at -4% 46%, ${colors.surface2}, transparent 24%)
          `,
        }}
      />

      {/* Grid field */}
      <GridField opacity={gridOpacity} mask="center" />

      {/* Flash overlay */}
      <AbsoluteFill
        style={{
          background: colors.accent,
          opacity: flashOpacity,
          pointerEvents: "none",
        }}
      />

      {/* Central content */}
      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 40,
        }}
      >
        {/* Kicker */}
        <div
          style={{
            opacity: kickerOpacity,
            transform: `translateY(${kickerY}px)`,
          }}
        >
          <Kicker>Shared memory for AI agents</Kicker>
        </div>

        {/* Logo */}
        <div
          style={{
            opacity: logoOpacity,
            transform: `scale(${logoScale})`,
          }}
        >
          <MeshLogo size={96} startFrame={8} />
        </div>

        {/* Tagline */}
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            justifyContent: "center",
            gap: 12,
            maxWidth: 800,
            textAlign: "center",
          }}
        >
          {taglineWords.map((word, i) => {
            const visible = wordProgress > i;
            return (
              <span
                key={i}
                style={{
                  fontFamily: fonts.body,
                  fontSize: 28,
                  color: colors.muted,
                  opacity: visible ? 1 : 0,
                  transform: `translateY(${visible ? 0 : 8}px)`,
                  letterSpacing: "-0.015em",
                }}
              >
                {word}
              </span>
            );
          })}
        </div>

        {/* Meta strip */}
        <div
          style={{
            display: "flex",
            gap: 20,
            fontFamily: fonts.mono,
            fontSize: 12,
            color: colors.muted,
            letterSpacing: "0.06em",
            opacity: interpolate(frame, [200, 240], [0, 0.7], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            }),
          }}
        >
          {["MIT", "local-first", "Python 3.10+", "no external database"].map((item, i) => (
            <React.Fragment key={i}>
              {i > 0 && <span style={{ color: colors.line }}>/</span>}
              <span>{item}</span>
            </React.Fragment>
          ))}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
}
