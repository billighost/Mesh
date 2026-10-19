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
          top: 140,
          left: 0,
          right: 0,
          display: "flex",
          justifyContent: "center",
          gap: 32,
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
                width: 380,
                background: colors.surface,
                border: `1px solid ${colors.line}`,
                borderRadius: radius.sm,
                boxShadow: `4px 4px 0px ${colors.ink}`,
                overflow: "hidden",
                fontFamily: fonts.mono,
              }}
            >
              {/* Title bar */}
              <div
                style={{
                  padding: "12px 18px",
                  borderBottom: `1px solid ${colors.line}`,
                  fontSize: 15,
                  color: colors.muted,
                  letterSpacing: "0.08em",
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  background: colors.surface2,
                }}
              >
                <span
                  style={{
                    width: 9,
                    height: 9,
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
                  padding: "18px 20px",
                  fontSize: 18,
                  color: colors.ink,
                  background: colors.surface,
                  minHeight: 120,
                  lineHeight: 1.6,
                }}
              >
                <span style={{ color: colors.accentText }}>$ </span>
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
          x1={480}
          y1={255}
          x2={1440}
          y2={255}
          stroke={failFlash > 0 ? colors.warn : colors.muted}
          strokeWidth={2.5}
          strokeDasharray={`${960 * lineProgress} 9999`}
          opacity={Math.max(lineProgress * 0.6, failFlash * 0.9)}
          strokeLinecap="round"
        />
        {/* Fail X mark */}
        {failFlash > 0 && (
          <g opacity={failFlash}>
            <text x={960} y={242} textAnchor="middle" fill={colors.warn} fontSize={28} fontFamily={fonts.mono} fontWeight={700}>
              ×
            </text>
          </g>
        )}
      </svg>

      {/* Headline text */}
      <div
        style={{
          position: "absolute",
          bottom: 150,
          left: 0,
          right: 0,
          textAlign: "center",
          padding: "0 60px",
        }}
      >
        {/* Line 1 */}
        <div
          style={{
            fontFamily: fonts.display,
            fontWeight: 700,
            fontSize: 88,
            lineHeight: 1.05,
            letterSpacing: "-0.045em",
            color: colors.ink,
            display: "flex",
            justifyContent: "center",
            gap: 18,
            flexWrap: "wrap",
            marginBottom: 10,
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
            fontWeight: 700,
            fontSize: 88,
            lineHeight: 1.05,
            letterSpacing: "-0.045em",
            display: "flex",
            justifyContent: "center",
            gap: 18,
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
            marginTop: 32,
            fontFamily: fonts.body,
            fontSize: 32,
            fontWeight: 500,
            lineHeight: 1.35,
            color: colors.muted,
            opacity: subOpacity,
            letterSpacing: "-0.015em",
            maxWidth: 1100,
            marginLeft: "auto",
            marginRight: "auto",
          }}
        >
          Claude doesn't know what Cursor discovered. Repeat work. Every. Single. Session.
        </p>
      </div>
    </AbsoluteFill>
  );
}
