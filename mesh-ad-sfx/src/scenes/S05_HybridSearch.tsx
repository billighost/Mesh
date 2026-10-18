import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts, radius } from "../lib/tokens";
import GridField from "../components/GridField";
import Kicker from "../components/Kicker";

const stages = [
  {
    num: "01",
    label: "Pre-mask filters",
    detail: "type · tags · confidence — applied before candidate cut",
    visual: "chips",
  },
  {
    num: "02",
    label: "Dense vector cosine",
    detail: "cache.matrix @ q_vec  ·  384-dim float32  ·  SIMD",
    visual: "matrix",
    badge: "< 0.1 ms",
  },
  {
    num: "03",
    label: "Lexical IDF + synonyms",
    detail: "Robertson-Spärck Jones IDF  ·  url ≈ link  ·  db ≈ database",
    visual: "synonyms",
  },
  {
    num: "04",
    label: "Lexical grounding floor",
    detail: "Exact and stem matches get a minimum similarity floor",
    visual: "bar",
  },
  {
    num: "05",
    label: "Match-reason tagging",
    detail: "Results stamped: exact+hybrid · lexical+semantic · semantic",
    visual: "tag",
  },
];

/**
 * Scene 5 — 5-Stage Hybrid Search (frames 0–600)
 * Five stages cascade in one by one with lime checkmarks.
 */
export default function S05_HybridSearch() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const kickerOpacity = interpolate(frame, [0, 24], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Counter in top-right
  const counterOpacity = interpolate(frame, [120, 160], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const counterPulse = interpolate(
    frame,
    [160, 175, 190],
    [1, 1.15, 1],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

  return (
    <AbsoluteFill style={{ background: colors.bg }}>
      <GridField opacity={0.2} mask="center" />

      {/* Kicker */}
      <div style={{ position: "absolute", top: 64, left: 80, opacity: kickerOpacity }}>
        <Kicker>5-Stage Hybrid Search</Kicker>
      </div>

      {/* Speed counter */}
      <div
        style={{
          position: "absolute",
          top: 64,
          right: 80,
          opacity: counterOpacity,
          transform: `scale(${counterPulse})`,
          fontFamily: fonts.mono,
          fontSize: 13,
          color: colors.accent,
          letterSpacing: "0.06em",
        }}
      >
        &lt; 0.1 ms
      </div>

      {/* Stages list */}
      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          padding: "100px 160px 80px",
          gap: 20,
        }}
      >
        {stages.map((stage, i) => {
          const stageStart = 30 + i * 80;
          const s = spring({
            frame: frame - stageStart,
            fps,
            config: { damping: 22, stiffness: 130, mass: 0.6 },
          });
          const opacity = interpolate(s, [0, 1], [0, 1]);
          const tx = interpolate(s, [0, 1], [-40, 0]);

          const checkOpacity = interpolate(
            frame,
            [stageStart + 30, stageStart + 50],
            [0, 1],
            { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
          );

          // Stage 4: animated fill bar
          const barProgress = interpolate(
            frame,
            [stageStart + 20, stageStart + 60],
            [0, 1],
            { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
          );

          return (
            <div
              key={i}
              style={{
                opacity,
                transform: `translateX(${tx}px)`,
                display: "flex",
                alignItems: "flex-start",
                gap: 24,
              }}
            >
              {/* Stage number */}
              <span
                style={{
                  fontFamily: fonts.mono,
                  fontSize: 11,
                  fontWeight: 500,
                  color: colors.muted,
                  letterSpacing: "0.1em",
                  marginTop: 3,
                  flexShrink: 0,
                  width: 26,
                }}
              >
                {stage.num}
              </span>

              {/* Content */}
              <div style={{ flex: 1 }}>
                <div
                  style={{
                    fontFamily: fonts.display,
                    fontWeight: 600,
                    fontSize: 22,
                    color: colors.ink,
                    letterSpacing: "-0.03em",
                    marginBottom: 4,
                  }}
                >
                  {stage.label}
                  {stage.badge && (
                    <span
                      style={{
                        marginLeft: 14,
                        fontFamily: fonts.mono,
                        fontSize: 12,
                        fontWeight: 500,
                        color: colors.accentInk,
                        background: colors.accent,
                        padding: "2px 9px",
                        borderRadius: radius.sm,
                        letterSpacing: "0.04em",
                        verticalAlign: "middle",
                      }}
                    >
                      {stage.badge}
                    </span>
                  )}
                </div>
                <div
                  style={{
                    fontFamily: fonts.mono,
                    fontSize: 13,
                    color: colors.muted,
                    letterSpacing: "0.02em",
                  }}
                >
                  {stage.detail}
                </div>

                {/* Stage 4: animated bar */}
                {stage.visual === "bar" && (
                  <div
                    style={{
                      marginTop: 10,
                      height: 3,
                      width: 280,
                      background: colors.surface2,
                      borderRadius: 2,
                      overflow: "hidden",
                    }}
                  >
                    <div
                      style={{
                        height: "100%",
                        width: `${barProgress * 100}%`,
                        background: colors.accent,
                        borderRadius: 2,
                      }}
                    />
                  </div>
                )}

                {/* Stage 5: result tag */}
                {stage.visual === "tag" && (
                  <div style={{ marginTop: 10 }}>
                    <span
                      style={{
                        fontFamily: fonts.mono,
                        fontSize: 11,
                        color: colors.accent,
                        border: `1px solid ${colors.accent}`,
                        padding: "2px 8px",
                        borderRadius: radius.sm,
                        letterSpacing: "0.06em",
                        opacity: checkOpacity,
                      }}
                    >
                      exact+hybrid
                    </span>
                  </div>
                )}
              </div>

              {/* Check */}
              <span
                style={{
                  opacity: checkOpacity,
                  fontFamily: fonts.mono,
                  fontSize: 18,
                  color: colors.accent,
                  flexShrink: 0,
                  marginTop: 2,
                }}
              >
                ✓
              </span>
            </div>
          );
        })}
      </AbsoluteFill>
    </AbsoluteFill>
  );
}
