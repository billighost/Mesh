import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { colors, fonts, MOBILE, radius } from "../lib/tokens";
import GridField from "../components/GridField";

const agents = [
  {
    name: "claude / terminal",
    id: "claude",
    cmd: "$ agent.work() // discovered postgres port 5432",
  },
  {
    name: "cursor",
    id: "cursor",
    cmd: "$ agent.work() // error: what port is postgres?",
  },
  {
    name: "gemini-cli",
    id: "gemini",
    cmd: "$ agent.work() // re-scanning config from scratch...",
  },
];

/**
 * Scene 1 — The Problem (frames 0–540 within sequence)
 * Three agent windows stack vertically, connection attempt fails at frame 90, amnesia copy reveals.
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

  // Connection line attempt: starts at frame 60, fails at frame 90 (synced with connection_glitch.wav)
  const lineProgress = interpolate(frame, [60, 85], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const failFlash = interpolate(frame, [90, 95, 135, 150], [0, 1, 1, 0.35], {
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
  const crumpleScale = interpolate(frame, [crumpleStart, crumpleStart + 30], [1, 0.88], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill style={{ background: colors.bg }}>
      <GridField opacity={0.28} mask="top" />

      {/* Vertical stack of 3 isolated agent windows */}
      <div
        style={{
          position: "absolute",
          top: MOBILE.safeTop,
          left: MOBILE.padX,
          right: MOBILE.padX,
          display: "flex",
          flexDirection: "column",
          gap: 36,
          opacity: crumpleOpacity,
          transform: `scale(${crumpleScale})`,
        }}
      >
        {agents.map((agent, i) => {
          const s = agentSprings[i];
          const opacity = interpolate(s, [0, 1], [0, 1]);
          const ty = interpolate(s, [0, 1], [28, 0]);
          const isGlitching = frame >= 90 && frame <= 125;

          return (
            <div
              key={agent.id}
              style={{
                opacity,
                transform: `translateY(${ty}px)`,
                width: "100%",
                background: colors.surface,
                border: `2px solid ${isGlitching ? colors.warn : colors.line}`,
                borderRadius: radius.md,
                boxShadow: "6px 6px 0px #000",
                overflow: "hidden",
                fontFamily: fonts.mono,
              }}
            >
              {/* Title bar */}
              <div
                style={{
                  padding: "14px 22px",
                  borderBottom: `1.5px solid ${colors.terminalLine}`,
                  fontSize: 20,
                  fontWeight: 600,
                  color: colors.muted,
                  letterSpacing: "0.06em",
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                  background: colors.surface2,
                }}
              >
                <span
                  style={{
                    width: 11,
                    height: 11,
                    borderRadius: "50%",
                    background: frame >= 90 ? colors.warn : colors.line,
                    display: "inline-block",
                  }}
                />
                <span>{agent.name}</span>
                <span
                  style={{
                    marginLeft: "auto",
                    fontSize: 16,
                    color: frame >= 90 ? colors.warn : colors.muted,
                  }}
                >
                  {frame >= 90 ? "✕ isolated session" : "● running"}
                </span>
              </div>

              {/* Terminal content */}
              <div
                style={{
                  padding: "22px 26px",
                  fontSize: 23,
                  color: colors.terminalInk,
                  background: colors.terminalBg,
                  lineHeight: 1.5,
                }}
              >
                <span style={{ color: colors.accent, fontWeight: 700 }}>$ </span>
                <span>{agent.cmd.replace(/^\$\s*/, "")}</span>
              </div>
            </div>
          );
        })}
      </div>

      {/* Failed vertical connection spine (SVG overlay) */}
      <svg
        style={{
          position: "absolute",
          inset: 0,
          width: "100%",
          height: "100%",
          pointerEvents: "none",
        }}
      >
        <line
          x1={MOBILE.centerX}
          y1={260}
          x2={MOBILE.centerX}
          y2={680}
          stroke={failFlash > 0 ? colors.warn : colors.muted}
          strokeWidth={4}
          strokeDasharray={`${420 * lineProgress} 9999`}
          opacity={Math.max(lineProgress * 0.65, failFlash * 0.95) * crumpleOpacity}
          strokeLinecap="round"
        />
      </svg>

      {/* Glitch failure badge */}
      {frame >= 90 && (
        <div
          style={{
            position: "absolute",
            top: 445,
            left: "50%",
            transform: "translateX(-50%)",
            opacity: failFlash * crumpleOpacity,
            background: colors.warnSoft,
            border: `2px solid ${colors.warn}`,
            color: colors.warn,
            fontFamily: fonts.mono,
            fontSize: 22,
            fontWeight: 700,
            padding: "10px 26px",
            borderRadius: radius.sm,
            letterSpacing: "0.08em",
            boxShadow: "0 10px 30px rgba(0,0,0,0.8)",
            whiteSpace: "nowrap",
          }}
        >
          ✕ NO SHARED CONTEXT
        </div>
      )}

      {/* Headline text block */}
      <div
        style={{
          position: "absolute",
          bottom: 240,
          left: MOBILE.padX,
          right: MOBILE.padX,
          textAlign: "center",
        }}
      >
        {/* Line 1 */}
        <div
          style={{
            fontFamily: fonts.display,
            fontWeight: 800,
            fontSize: 70,
            lineHeight: 1.08,
            letterSpacing: "-0.045em",
            color: colors.ink,
            display: "flex",
            justifyContent: "center",
            gap: 18,
            flexWrap: "wrap",
            marginBottom: 14,
          }}
        >
          {line1Words.map((word, i) => {
            const visible = wordIndex > i;
            return (
              <span
                key={i}
                style={{
                  opacity: visible ? 1 : 0,
                  transform: `translateY(${visible ? 0 : 16}px)`,
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
            fontWeight: 800,
            fontSize: 70,
            lineHeight: 1.08,
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
                  transform: `translateY(${visible ? 0 : 16}px)`,
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
            marginTop: 40,
            fontFamily: fonts.body,
            fontSize: 38,
            fontWeight: 500,
            lineHeight: 1.38,
            color: colors.muted,
            opacity: subOpacity,
            letterSpacing: "-0.015em",
          }}
        >
          Claude doesn’t know what Cursor discovered.{" "}
          <span style={{ color: colors.ink, fontWeight: 600 }}>
            Repeat work. Every. Single. Session.
          </span>
        </p>
      </div>
    </AbsoluteFill>
  );
}