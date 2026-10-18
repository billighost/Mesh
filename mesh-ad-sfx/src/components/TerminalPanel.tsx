import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts, radius } from "../lib/tokens";
import { useTypewriterLines } from "../hooks/useTypewriter";

interface TerminalPanelProps {
  title?: string;
  lines: string[];
  /** Frame to start typing */
  startFrame?: number;
  width?: number | string;
  /** chars per frame typing speed */
  speed?: number;
  /** Frame for panel entrance spring */
  entranceFrame?: number;
  style?: React.CSSProperties;
}

export default function TerminalPanel({
  title = "mesh",
  lines,
  startFrame = 0,
  width = 560,
  speed = 0.9,
  entranceFrame = 0,
  style,
}: TerminalPanelProps) {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const entranceSpring = spring({
    frame: frame - entranceFrame,
    fps,
    config: { damping: 24, stiffness: 120, mass: 0.7 },
  });

  const opacity = interpolate(entranceSpring, [0, 1], [0, 1]);
  const translateY = interpolate(entranceSpring, [0, 1], [24, 0]);

  const typedLines = useTypewriterLines(lines, startFrame, speed);

  // Blinking cursor: on for 30 frames, off for 30
  const cursorVisible = Math.floor(frame / 30) % 2 === 0;

  return (
    <div
      style={{
        width,
        opacity,
        transform: `translateY(${translateY}px)`,
        background: colors.terminalBg,
        border: `1px solid ${colors.line}`,
        borderRadius: radius.sm,
        boxShadow: `4px 4px 0px #000`,
        overflow: "hidden",
        fontFamily: fonts.mono,
        fontSize: 14,
        lineHeight: 1.6,
        ...style,
      }}
    >
      {/* Title bar */}
      <div
        style={{
          padding: "10px 16px",
          borderBottom: `1px solid ${colors.terminalLine}`,
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        {/* Traffic-light dots */}
        {["#3D3D3D", "#3D3D3D", "#3D3D3D"].map((c, i) => (
          <span
            key={i}
            style={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              background: c,
              display: "inline-block",
            }}
          />
        ))}
        <span
          style={{
            marginLeft: 8,
            color: colors.terminalDim,
            fontSize: 11,
            letterSpacing: "0.08em",
            fontFamily: fonts.mono,
          }}
        >
          {title}
        </span>
      </div>

      {/* Content */}
      <div style={{ padding: "16px 20px", minHeight: 140 }}>
        {typedLines.map((line, i) => {
          const isPrompt = line.startsWith("$") || line.startsWith(">>>");
          const isOk = line.startsWith("ok ");
          const isResult = line.startsWith("[");
          const isComment = line.startsWith("#");

          let color: string = colors.terminalInk;
          if (isPrompt) color = colors.accent;
          if (isOk) color = colors.muted;
          if (isResult) color = colors.accent;
          if (isComment) color = colors.terminalDim;

          return (
            <div key={i} style={{ color, whiteSpace: "pre" }}>
              {line}
              {/* Blinking cursor on last line if still typing */}
              {i === typedLines.length - 1 && cursorVisible && (
                <span style={{ color: colors.accent }}>▌</span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
