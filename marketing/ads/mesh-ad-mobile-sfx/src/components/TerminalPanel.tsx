import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts, MOBILE, radius } from "../lib/tokens";
import { useTypewriterLines } from "../hooks/useTypewriter";

interface TerminalPanelProps {
  title?: string;
  badge?: string;
  lines: string[];
  startFrame?: number;
  width?: number | string;
  speed?: number;
  entranceFrame?: number;
  fontSize?: number;
  minHeight?: number;
  style?: React.CSSProperties;
}

export default function TerminalPanel({
  title = "mesh",
  badge,
  lines,
  startFrame = 0,
  width = MOBILE.contentWidth,
  speed = 0.9,
  entranceFrame = 0,
  fontSize = 23,
  minHeight = 220,
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
  const translateY = interpolate(entranceSpring, [0, 1], [28, 0]);

  const typedLines = useTypewriterLines(lines, startFrame, speed);
  const cursorVisible = Math.floor(frame / 24) % 2 === 0;

  return (
    <div
      style={{
        width,
        opacity,
        transform: `translateY(${translateY}px)`,
        background: colors.terminalBg,
        border: `1.5px solid ${colors.line}`,
        borderRadius: radius.md,
        boxShadow: "6px 6px 0px #000",
        overflow: "hidden",
        fontFamily: fonts.mono,
        fontSize,
        lineHeight: 1.62,
        ...style,
      }}
    >
      {/* Title bar */}
      <div
        style={{
          padding: "16px 24px",
          borderBottom: `1.5px solid ${colors.terminalLine}`,
          display: "flex",
          alignItems: "center",
          gap: 12,
          background: colors.surface2,
        }}
      >
        {["#3D3D3D", "#3D3D3D", "#3D3D3D"].map((c, i) => (
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
            marginLeft: 8,
            color: colors.terminalDim,
            fontSize: 19,
            fontWeight: 600,
            letterSpacing: "0.06em",
            fontFamily: fonts.mono,
          }}
        >
          {title}
        </span>
        {badge && (
          <span
            style={{
              marginLeft: "auto",
              fontSize: 16,
              fontWeight: 700,
              color: colors.accent,
              letterSpacing: "0.08em",
              textTransform: "uppercase",
              border: `1px solid ${colors.accent}`,
              background: colors.accentSoft,
              padding: "3px 10px",
              borderRadius: radius.sm,
            }}
          >
            {badge}
          </span>
        )}
      </div>

      {/* Content */}
      <div style={{ padding: "26px 30px", minHeight }}>
        {typedLines.map((line, i) => {
          const isPrompt = line.startsWith("$") || line.startsWith(">>>") || line.startsWith("...");
          const isOk = line.startsWith("ok ");
          const isResult = line.startsWith("[");
          const isComment = line.startsWith("#");

          let color: string = colors.terminalInk;
          if (isPrompt) color = colors.accent;
          if (isOk) color = colors.muted;
          if (isResult) color = colors.accent;
          if (isComment) color = colors.terminalDim;

          return (
            <div
              key={i}
              style={{
                color,
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                fontWeight: isPrompt || isResult ? 600 : 400,
                marginBottom: 4,
              }}
            >
              {line}
              {i === typedLines.length - 1 && cursorVisible && (
                <span style={{ color: colors.accent, marginLeft: 2 }}>█</span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}