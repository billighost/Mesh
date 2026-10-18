import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts, radius } from "../lib/tokens";
import GridField from "../components/GridField";
import Kicker from "../components/Kicker";
import { useTypewriterLines } from "../hooks/useTypewriter";

const installLines = [
  "$ pip install mesh-context-layer",
  "Successfully installed mesh-context-layer-0.7.1",
  "$ mesh setup",
  "ok  sqlite wal / fts5 / 384d onnx",
  "ok  daemon started on :7701",
  "ok  skills installed",
  "ok  _",
];

/**
 * Scene 11 — Install (frames 0–300)
 * Minimal: two commands, one terminal, "zero config".
 */
export default function S11_Install() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const kickerOpacity = interpolate(frame, [0, 24], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const terminalSpring = spring({
    frame: frame - 15,
    fps,
    config: { damping: 20, stiffness: 100, mass: 0.9 },
  });
  const termOpacity = interpolate(terminalSpring, [0, 1], [0, 1]);
  const termScale = interpolate(terminalSpring, [0, 1], [0.95, 1]);

  const typedLines = useTypewriterLines(installLines, 40, 0.9);

  // Blinking cursor
  const cursorVisible = Math.floor(frame / 30) % 2 === 0;

  // Caption
  const captionOpacity = interpolate(frame, [230, 265], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill style={{ background: colors.bg }}>
      <GridField opacity={0.25} mask="center" />

      <div style={{ position: "absolute", top: 64, left: 80, opacity: kickerOpacity }}>
        <Kicker>Get started</Kicker>
      </div>

      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 36,
        }}
      >
        {/* Terminal */}
        <div
          style={{
            opacity: termOpacity,
            transform: `scale(${termScale})`,
            width: 600,
            background: colors.surface,
            border: `1px solid ${colors.line}`,
            borderRadius: radius.sm,
            boxShadow: `4px 4px 0px ${colors.ink}`,
            fontFamily: fonts.mono,
            fontSize: 14,
            overflow: "hidden",
          }}
        >
          {/* Title bar */}
          <div
            style={{
              padding: "10px 16px",
              background: colors.surface2,
              borderBottom: `1px solid ${colors.line}`,
              display: "flex",
              alignItems: "center",
              gap: 8,
            }}
          >
            {["#C5CBB9", "#C5CBB9", "#C5CBB9"].map((c, i) => (
              <span key={i} style={{ width: 8, height: 8, borderRadius: "50%", background: c, display: "inline-block" }} />
            ))}
            <span style={{ marginLeft: 8, color: colors.muted, fontSize: 11, letterSpacing: "0.08em" }}>
              shell
            </span>
          </div>

          {/* Lines */}
          <div style={{ padding: "16px 20px", background: colors.surface, minHeight: 180, lineHeight: 1.7 }}>
            {typedLines.map((line, i) => {
              const isPrompt = line.startsWith("$");
              const isOk = line.startsWith("ok ");
              const isSuccess = line.includes("Successfully");

              let color: string = colors.ink;
              if (isPrompt) color = colors.accentText;
              if (isOk) color = colors.muted;
              if (isSuccess) color = colors.muted;

              return (
                <div key={i} style={{ color, whiteSpace: "pre" }}>
                  {line}
                  {i === typedLines.length - 1 && cursorVisible && (
                    <span style={{ color: colors.accentText }}>▌</span>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* Caption */}
        <div style={{ opacity: captionOpacity, textAlign: "center" }}>
          <span
            style={{
              fontFamily: fonts.display,
              fontWeight: 600,
              fontSize: 30,
              letterSpacing: "-0.035em",
              color: colors.ink,
            }}
          >
            Zero config.{" "}
            <span style={{ color: colors.muted }}>Two commands. Instant shared memory.</span>
          </span>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
}
