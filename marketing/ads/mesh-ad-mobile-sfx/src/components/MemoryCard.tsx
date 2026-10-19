import React from "react";
import { spring, useCurrentFrame, useVideoConfig, interpolate } from "remotion";
import { colors, fonts, MOBILE, radius } from "../lib/tokens";

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
  conflict?: boolean;
  resolved?: boolean;
  entranceFrame?: number;
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
  width = MOBILE.contentWidth,
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
  const scale = interpolate(s, [0, 1], [0.94, 1]);

  const slideOffsets = {
    left: interpolate(s, [0, 1], [-45, 0]),
    right: interpolate(s, [0, 1], [45, 0]),
    bottom: interpolate(s, [0, 1], [35, 0]),
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
        border: `2px solid ${borderColor}`,
        borderRadius: radius.md,
        boxShadow: "6px 6px 0px #000",
        padding: "28px 32px",
        fontFamily: fonts.mono,
        ...style,
      }}
    >
      {/* Type label */}
      <div
        style={{
          fontSize: 18,
          fontWeight: 600,
          textTransform: "uppercase",
          letterSpacing: "0.1em",
          color: colors.muted,
          marginBottom: 16,
          display: "flex",
          alignItems: "center",
          gap: 12,
          flexWrap: "wrap",
        }}
      >
        <span
          style={{
            width: 11,
            height: 11,
            borderRadius: "50%",
            background: conflict ? colors.warn : resolved ? colors.accent : colors.accent,
            display: "inline-block",
          }}
        />
        <span style={{ color: colors.ink }}>{memoryType}</span>
        {source && (
          <>
            <span style={{ color: colors.line }}>•</span>
            <span>{source}</span>
          </>
        )}
        {similarity !== undefined && (
          <>
            <span style={{ color: colors.line }}>•</span>
            <span style={{ color: colors.accent, fontWeight: 700 }}>
              {similarity.toFixed(2)} sim
            </span>
          </>
        )}
        {conflict && (
          <span
            style={{
              color: colors.warn,
              marginLeft: "auto",
              fontWeight: 700,
              background: colors.warnSoft,
              border: `1px solid ${colors.warn}`,
              padding: "3px 12px",
              borderRadius: radius.sm,
            }}
          >
            ⚠ conflict
          </span>
        )}
        {resolved && (
          <span
            style={{
              color: colors.accent,
              marginLeft: "auto",
              fontWeight: 700,
              background: colors.accentSoft,
              border: `1px solid ${colors.accent}`,
              padding: "3px 12px",
              borderRadius: radius.sm,
            }}
          >
            ✓ resolved
          </span>
        )}
      </div>

      {/* Content */}
      <div
        style={{
          fontSize: 27,
          fontWeight: 600,
          color: colors.ink,
          lineHeight: 1.45,
          fontFamily: fonts.mono,
          marginBottom: tags.length ? 20 : 0,
          wordBreak: "break-word",
        }}
      >
        {content}
      </div>

      {/* Tags */}
      {tags.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
          {tags.map((tag, i) => (
            <span
              key={i}
              style={{
                fontFamily: fonts.mono,
                fontSize: 17,
                fontWeight: 600,
                textTransform: "uppercase",
                letterSpacing: "0.08em",
                padding: "6px 14px",
                border: `1.5px solid ${tag.variant === "accent" ? colors.accent : colors.line}`,
                borderRadius: radius.sm,
                color: tag.variant === "accent" ? colors.accent : colors.muted,
                background:
                  tag.variant === "accent" ? colors.accentSoft : colors.surface2,
              }}
            >
              #{tag.label}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}