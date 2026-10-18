import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { colors, fonts, radius } from "../lib/tokens";
import GridField from "../components/GridField";

const agents = [
  { name: "claude / terminal", id: "claude" },
  { name: "cursor", id: "cursor" },
  { name: "gemini-cli", id: "gemini" },
];

/**
 * Scene 1 — The Problem (frames 0–540 within the sequence)
 * Three agent windows appear, a connection attempt fails, amnesia copy reveals.
 */
export default function S01_Problem() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Each agent window springs in staggered
  const agentSprings = agents.map((_, i) =>
    spring({
      frame: frame - i * 12,
      fps,
      config: { damping: 22, stiffness: 120, mass: 0.7 },
    })
  );

  // Connection line attempt: starts at frame 60, fails at frame 90
  const lineProgress = interpolate(frame, [60, 85], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const failFlash = interpolate(frame, [90, 95, 110, 115], [0, 1, 1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Headline words appear one by one
  const line1Words = ["Your", "agents", "are", "brilliant."];
  const line2Words = ["And", "they", "have", "amnesia."];
  const allWords = [...line1Words, ...line2Words];
  const wordIndex = interpolate(frame, [100, 200], [0, allWords.length], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Sub-text fades in
  const subOpacity = interpolate(frame, [210, 250], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Agent windows crumple out at the end
  const crumpleStart = 460;
  const crumpleOpacity = interpolate(frame, [crumpleStart, crumpleStart + 30], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const crumpleScale = interpolate(frame, [crumpleStart, crumpleStart + 30], [1, 0.85], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill style={{ background: colors.bg }}>
      <GridField opacity={0.25} mask="top" />

      {/* Agent windows row */}
      <div
        style={{
          position: "absolute",
          top: 160,
          left: 0,
          right: 0,
          display: "flex",
          justifyContent: "center",
          gap: 28,
          opacity: crumpleOpacity,
          transform: `scale(${crumpleScale})`,
        }}
      >
        {agents.map((agent, i) => {
          const s = agentSprings[i];
          const opacity = interpolate(s, [0, 1], [0, 1]);
          const ty = interpolate(s, [0, 1], [30, 0]);

          return (
            <div
              key={agent.id}
              style={{
                opacity,
                transform: `translateY(${ty}px)`,
                width: 280,
                background: colors.surface,
                border: `1px solid ${colors.line}`,
                borderRadius: radius.sm,
                boxShadow: "4px 4px 0px #000",
                overflow: "hidden",
                fontFamily: fonts.mono,
              }}
            >
              {/* Title bar */}
              <div
                style={{
                  padding: "10px 14px",
                  borderBottom: `1px solid ${colors.terminalLine}`,
                  fontSize: 10,
                  color: colors.muted,
                  letterSpacing: "0.08em",
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  background: colors.terminalBg,
                }}
              >
                <span
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: "50%",
                    background: colors.line,
                    display: "inline-block",
                  }}
                />
                {agent.name}
              </div>
              {/* Terminal content */}
              <div
                style={{
                  padding: "14px 16px",
                  fontSize: 12,
                  color: colors.terminalDim,
                  background: colors.terminalBg,
                  minHeight: 100,
                }}
              >
                <span style={{ color: colors.accent }}>$ </span>
                <span>agent.work()▌</span>
              </div>
            </div>
          );
        })}
      </div>

      {/* Failed connection line (SVG overlay) */}
      <svg
        style={{ position: "absolute", inset: 0, width: "100%", height: "100%", pointerEvents: "none" }}
      >
        {/* Line from agent 0 to agent 2 */}
        <line
          x1={540}
          y1={260}
          x2={1380}
          y2={260}
          stroke={failFlash > 0 ? colors.warn : colors.muted}
          strokeWidth={1.5}
          strokeDasharray={`${840 * lineProgress} 9999`}
          opacity={Math.max(lineProgress * 0.6, failFlash * 0.9)}
          strokeLinecap="round"
        />
        {/* Fail X mark */}
        {failFlash > 0 && (
          <g opacity={failFlash}>
            <text x={955} y={248} textAnchor="middle" fill={colors.warn} fontSize={18} fontFamily={fonts.mono}>
              ×
            </text>
          </g>
        )}
      </svg>

      {/* Headline text */}
      <div
        style={{
          position: "absolute",
          bottom: 200,
          left: 0,
          right: 0,
          textAlign: "center",
          padding: "0 80px",
        }}
      >
        {/* Line 1 */}
        <div
          style={{
            fontFamily: fonts.display,
            fontWeight: 600,
            fontSize: 72,
            lineHeight: 1.05,
            letterSpacing: "-0.045em",
            color: colors.ink,
            display: "flex",
            justifyContent: "center",
            gap: 16,
            flexWrap: "wrap",
            marginBottom: 8,
          }}
        >
          {line1Words.map((word, i) => {
            const visible = wordIndex > i;
            return (
              <span
                key={i}
                style={{
                  opacity: visible ? 1 : 0,
                  transform: `translateY(${visible ? 0 : 12}px)`,
                  transition: "opacity 0.2s, transform 0.2s",
                  color: word === "brilliant." ? colors.ink : colors.ink,
                }}
              >
                {word}
              </span>
            );
          })}
        </div>

        {/* Line 2 */}
        <div
          style={{
            fontFamily: fonts.display,
            fontWeight: 600,
            fontSize: 72,
            lineHeight: 1.05,
            letterSpacing: "-0.045em",
            display: "flex",
            justifyContent: "center",
            gap: 16,
            flexWrap: "wrap",
          }}
        >
          {line2Words.map((word, i) => {
            const visible = wordIndex > line1Words.length + i;
            const isAmnesia = word === "amnesia.";
            return (
              <span
                key={i}
                style={{
                  opacity: visible ? 1 : 0,
                  transform: `translateY(${visible ? 0 : 12}px)`,
                  color: isAmnesia ? colors.warn : colors.ink,
                }}
              >
                {word}
              </span>
            );
          })}
        </div>

        {/* Sub-text */}
        <p
          style={{
            marginTop: 28,
            fontFamily: fonts.body,
            fontSize: 20,
            color: colors.muted,
            opacity: subOpacity,
            letterSpacing: "-0.01em",
          }}
        >
          Claude doesn't know what Cursor discovered. Repeat work. Every. Single. Session.
        </p>
      </div>
    </AbsoluteFill>
  );
}
