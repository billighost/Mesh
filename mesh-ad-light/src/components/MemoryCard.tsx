import React from "react";
import { spring, useCurrentFrame, useVideoConfig, interpolate } from "remotion";
import { colors, fonts, radius } from "../lib/tokens";

interface Tag {
  label: string;
  variant?: "accent" | "muted";
}

interface MemoryCardProps {
  content: string;
  memoryType?: string;
  tags?: Tag[];
  source?: string;
  similarity?: number;
  /** conflict: shows amber warning border */
  conflict?: boolean;
  /** resolved: shows lime border */
  resolved?: boolean;
  /** Frame for spring entrance */
  entranceFrame?: number;
  /** Horizontal slide direction for entrance */
  slideFrom?: "left" | "right" | "bottom" | "none";
  width?: number | string;
  style?: React.CSSProperties;
}

export default function MemoryCard({
  content,
  memoryType = "fact",
  tags = [],
  source,
  similarity,
  conflict = false,
  resolved = false,
  entranceFrame = 0,
  slideFrom = "bottom",
  width = 580,
  style,
}: MemoryCardProps) {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const s = spring({
    frame: frame - entranceFrame,
    fps,
    config: { damping: 22, stiffness: 130, mass: 0.6 },
  });

  const opacity = interpolate(s, [0, 1], [0, 1]);
  const scale = interpolate(s, [0, 1], [0.93, 1]);

  const slideOffsets = {
    left: interpolate(s, [0, 1], [-40, 0]),
    right: interpolate(s, [0, 1], [40, 0]),
    bottom: interpolate(s, [0, 1], [30, 0]),
    none: 0,
  };

  const tx = slideFrom === "left" || slideFrom === "right" ? slideOffsets[slideFrom] : 0;
  const ty = slideFrom === "bottom" ? slideOffsets.bottom : 0;

  const borderColor = conflict
    ? colors.warn
    : resolved
    ? colors.accent
    : colors.line;

  return (
    <div
      style={{
        width,
        opacity,
        transform: `scale(${scale}) translate(${tx}px, ${ty}px)`,
        background: colors.surface,
        border: `1px solid ${borderColor}`,
        borderRadius: radius.sm,
        boxShadow: `4px 4px 0px #000`,
        padding: "22px 26px",
        fontFamily: fonts.mono,
        ...style,
      }}
    >
      {/* Type label */}
      <div
        style={{
          fontSize: 15,
          fontWeight: 600,
          textTransform: "uppercase",
          letterSpacing: "0.12em",
          color: colors.muted,
          marginBottom: 12,
          display: "flex",
          alignItems: "center",
          gap: 10,
        }}
      >
        <span
          style={{
            width: 9,
            height: 9,
            borderRadius: "50%",
            background: conflict ? colors.warn : resolved ? colors.accent : colors.muted,
            display: "inline-block",
          }}
        />
        {memoryType}
        {source && (
          <>
            <span style={{ color: colors.line }}>·</span>
            <span>{source}</span>
          </>
        )}
        {similarity !== undefined && (
          <>
            <span style={{ color: colors.line }}>·</span>
            <span style={{ color: colors.accentText }}>
              {similarity.toFixed(2)} sim
            </span>
          </>
        )}
        {conflict && (
          <span style={{ color: colors.warn, marginLeft: "auto", fontWeight: 700 }}>⚠ conflict</span>
        )}
        {resolved && (
          <span style={{ color: colors.accentText, marginLeft: "auto", fontWeight: 700 }}>✓ resolved</span>
        )}
      </div>

      {/* Content */}
      <div
        style={{
          fontSize: 22,
          color: colors.ink,
          lineHeight: 1.55,
          fontFamily: fonts.mono,
          marginBottom: tags.length ? 16 : 0,
        }}
      >
        {content}
      </div>

      {/* Tags */}
      {tags.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {tags.map((tag, i) => (
            <span
              key={i}
              style={{
                fontFamily: fonts.mono,
                fontSize: 14,
                fontWeight: 600,
                textTransform: "uppercase",
                letterSpacing: "0.08em",
                padding: "4px 10px",
                border: `1px solid ${tag.variant === "accent" ? colors.accent : colors.line}`,
                borderRadius: radius.sm,
                color: tag.variant === "accent" ? colors.accentText : colors.muted,
                background:
                  tag.variant === "accent" ? colors.accentSoft : colors.surface2,
              }}
            >
              {tag.label}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
