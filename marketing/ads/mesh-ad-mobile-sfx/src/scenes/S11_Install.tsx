import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts, MOBILE, radius } from "../lib/tokens";
import GridField from "../components/GridField";
import Kicker from "../components/Kicker";
import { useTypewriterLines } from "../hooks/useTypewriter";

const installLines = [
  "$ pip install mesh-context-layer",
  "Successfully installed mesh-context-layer-0.7.1",
  "$ mesh setup",
  "ok  sqlite wal • fts5 • 384d onnx",
  "ok  daemon started on :7701",
  "ok  skills installed to 8 agents",
];

/**
 * Scene 11 — Install (frames 0–240 in MeshAd sequence)
 * Two commands, full-width mobile terminal, zero config.
 */
export default function S11_Install() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const kickerOpacity = interpolate(frame, [0, 20], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const terminalSpring = spring({
    frame: frame - 10,
    fps,
    config: { damping: 20, stiffness: 100, mass: 0.9 },
  });
  const termOpacity = interpolate(terminalSpring, [0, 1], [0, 1]);
  const termScale = interpolate(terminalSpring, [0, 1], [0.95, 1]);

  // Fast typing so all 6 lines finish right around frame 145 (global 4945 return key)
  const typedLines = useTypewriterLines(installLines, 25, 1.8);
  const cursorVisible = Math.floor(frame / 24) % 2 === 0;

  // Caption fades in at 145 and holds until 240
  const captionOpacity = interpolate(frame, [145, 175], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill style={{ background: colors.bg }}>
      <GridField opacity={0.28} mask="center" />

      {/* Header */}
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
        <Kicker>Get Started in 10 Seconds</Kicker>
        <span
          style={{
            fontFamily: fonts.mono,
            fontSize: 20,
            fontWeight: 700,
            color: colors.accent,
          }}
        >
          PYPI • v0.7.1
        </span>
      </div>

      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          padding: `0 ${MOBILE.padX}px`,
          gap: 44,
        }}
      >
        {/* Full-width Mobile Terminal */}
        <div
          style={{
            opacity: termOpacity,
            transform: `scale(${termScale})`,
            width: "100%",
            background: colors.terminalBg,
            border: `2px solid ${colors.accent}`,
            borderRadius: radius.md,
            boxShadow: "0 0 40px rgba(200, 240, 74, 0.15), 8px 8px 0px #000",
            fontFamily: fonts.mono,
            fontSize: 25,
            overflow: "hidden",
          }}
        >
          {/* Title bar */}
          <div
            style={{
              padding: "16px 24px",
              borderBottom: `1.5px solid ${colors.terminalLine}`,
              background: colors.surface2,
              display: "flex",
              alignItems: "center",
              gap: 12,
            }}
          >
            {["#4A4A4A", "#4A4A4A", "#4A4A4A"].map((c, i) => (
              <span
                key={i}
                style={{
                  width: 12,
                  height: 12,
                  borderRadius: "50%",
                  background: c,
                  display: "inline-block",
                }}
              />
            ))}
            <span
              style={{
                marginLeft: 10,
                color: colors.terminalDim,
                fontSize: 19,
                fontWeight: 600,
                letterSpacing: "0.08em",
              }}
            >
              zsh — quickstart
            </span>
          </div>

          {/* Lines */}
          <div style={{ padding: "32px 34px", minHeight: 390, lineHeight: 1.75 }}>
            {typedLines.map((line, i) => {
              const isPrompt = line.startsWith("$");
              const isOk = line.startsWith("ok ");
              const isSuccess = line.includes("Successfully");

              let color: string = colors.terminalInk;
              if (isPrompt) color = colors.accent;
              if (isOk) color = colors.ink;
              if (isSuccess) color = colors.muted;

              return (
                <div
                  key={i}
                  style={{
                    color,
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-word",
                    fontWeight: isPrompt ? 700 : 500,
                    marginBottom: 6,
                  }}
                >
                  {line}
                  {i === typedLines.length - 1 && cursorVisible && (
                    <span style={{ color: colors.accent, marginLeft: 4 }}>█</span>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* Auto-skill installation callout */}
        <div
          style={{
            opacity: captionOpacity,
            width: "100%",
            background: colors.surface,
            border: `1.5px solid ${colors.line}`,
            borderRadius: radius.md,
            padding: "20px 28px",
            fontFamily: fonts.mono,
            fontSize: 21,
            color: colors.muted,
            textAlign: "center",
            lineHeight: 1.5,
          }}
        >
          <span style={{ color: colors.accent, fontWeight: 700 }}>✓ Auto-installs skills</span>{" "}
          into Claude Code, Cursor, Gemini CLI, Windsurf, Codex, OpenCode &amp; Antigravity
        </div>

        {/* Caption */}
        <div style={{ opacity: captionOpacity, textAlign: "center" }}>
          <div
            style={{
              fontFamily: fonts.display,
              fontWeight: 800,
              fontSize: 52,
              letterSpacing: "-0.035em",
              color: colors.ink,
              lineHeight: 1.15,
            }}
          >
            Zero config.{" "}
            <span style={{ color: colors.accent }}>Two commands.</span>
          </div>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
}