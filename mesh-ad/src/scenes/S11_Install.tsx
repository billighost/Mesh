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
            width: 860,
            background: colors.terminalBg,
            border: `1px solid ${colors.line}`,
            borderRadius: radius.sm,
            boxShadow: "6px 6px 0px #000",
            fontFamily: fonts.mono,
            fontSize: 22,
            overflow: "hidden",
          }}
        >
          {/* Title bar */}
          <div
            style={{
              padding: "14px 22px",
              borderBottom: `1px solid ${colors.terminalLine}`,
              display: "flex",
              alignItems: "center",
              gap: 10,
            }}
          >
            {["#4A4A4A", "#4A4A4A", "#4A4A4A"].map((c, i) => (
              <span key={i} style={{ width: 11, height: 11, borderRadius: "50%", background: c, display: "inline-block" }} />
            ))}
            <span style={{ marginLeft: 10, color: colors.terminalDim, fontSize: 16, letterSpacing: "0.08em" }}>
              shell
            </span>
          </div>

          {/* Lines */}
          <div style={{ padding: "26px 30px", minHeight: 280, lineHeight: 1.8 }}>
            {typedLines.map((line, i) => {
              const isPrompt = line.startsWith("$");
              const isOk = line.startsWith("ok ");
              const isSuccess = line.includes("Successfully");

              let color: string = colors.terminalDim;
              if (isPrompt) color = colors.accent;
              if (isOk) color = colors.muted;
              if (isSuccess) color = colors.muted;

              return (
                <div key={i} style={{ color, whiteSpace: "pre", fontWeight: isPrompt ? 600 : 400 }}>
                  {line}
                  {i === typedLines.length - 1 && cursorVisible && (
                    <span style={{ color: colors.accent }}>▌</span>
                  )}
                </div>
              );
            })}
          </div>
        </div>

        {/* Caption */}
        <div style={{ opacity: captionOpacity, textAlign: "center", marginTop: 10 }}>
          <span
            style={{
              fontFamily: fonts.display,
              fontWeight: 700,
              fontSize: 42,
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
