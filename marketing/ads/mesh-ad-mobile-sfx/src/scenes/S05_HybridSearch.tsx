import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts, MOBILE, radius } from "../lib/tokens";
import GridField from "../components/GridField";
import Kicker from "../components/Kicker";

const stages = [
  {
    num: "01",
    label: "Pre-mask filters",
    detail: "type • tags • confidence → applied before candidate cut",
    visual: "chips",
  },
  {
    num: "02",
    label: "Dense vector cosine",
    detail: "cache.matrix @ q_vec • 384-dim float32 • SIMD",
    visual: "matrix",
    badge: "< 0.1 ms",
  },
  {
    num: "03",
    label: "Lexical IDF + synonyms",
    detail: "Robertson-Spärck Jones IDF • url ≈ link • db ≈ database",
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
    detail: "Results stamped: exact+hybrid • lexical+semantic • semantic",
    visual: "tag",
  },
];

/**
 * Scene 5 — 5-Stage Hybrid Search (frames 0–600)
 * Five stages cascade in vertically one by one with lime checkmarks.
 */
export default function S05_HybridSearch() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const kickerOpacity = interpolate(frame, [0, 24], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const counterOpacity = interpolate(frame, [120, 160], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const counterPulse = interpolate(frame, [160, 175, 190], [1, 1.15, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill style={{ background: colors.bg }}>
      <GridField opacity={0.22} mask="center" />

      {/* Header bar */}
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
        <Kicker>5-Stage Hybrid Search</Kicker>

        <div
          style={{
            opacity: counterOpacity,
            transform: `scale(${counterPulse})`,
            fontFamily: fonts.mono,
            fontSize: 24,
            fontWeight: 700,
            color: colors.accentInk,
            background: colors.accent,
            padding: "6px 18px",
            borderRadius: radius.sm,
            letterSpacing: "0.06em",
          }}
        >
          &lt; 0.1 ms
        </div>
      </div>

      {/* Vertical 5-Stage Card Stack */}
      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "column",
          justifyContent: "flex-start",
          padding: `${MOBILE.safeTop + 90}px ${MOBILE.padX}px ${MOBILE.safeBottom}px`,
          gap: 24,
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
          const ty = interpolate(s, [0, 1], [28, 0]);

          // Checkmark pops at stageStart + 30 (synced with stage_chime_1..5.wav)
          const checkOpacity = interpolate(
            frame,
            [stageStart + 30, stageStart + 50],
            [0, 1],
            { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
          );
          const isCompleted = frame >= stageStart + 30;

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
                transform: `translateY(${ty}px)`,
                display: "flex",
                alignItems: "flex-start",
                gap: 24,
                background: colors.surface,
                border: `2px solid ${isCompleted ? colors.accent : colors.line}`,
                borderRadius: radius.md,
                padding: "24px 28px",
                boxShadow: "6px 6px 0px #000",
              }}
            >
              {/* Stage number pill */}
              <div
                style={{
                  fontFamily: fonts.mono,
                  fontSize: 22,
                  fontWeight: 700,
                  color: isCompleted ? colors.accent : colors.muted,
                  background: isCompleted ? colors.accentSoft : colors.surface2,
                  border: `1.5px solid ${isCompleted ? colors.accent : colors.line}`,
                  borderRadius: radius.sm,
                  padding: "8px 14px",
                  letterSpacing: "0.08em",
                  flexShrink: 0,
                }}
              >
                {stage.num}
              </div>

              {/* Stage body */}
              <div style={{ flex: 1 }}>
                <div
                  style={{
                    fontFamily: fonts.display,
                    fontWeight: 700,
                    fontSize: 33,
                    color: colors.ink,
                    letterSpacing: "-0.03em",
                    marginBottom: 8,
                    display: "flex",
                    alignItems: "center",
                    flexWrap: "wrap",
                    gap: 14,
                  }}
                >
                  <span>{stage.label}</span>
                  {stage.badge && (
                    <span
                      style={{
                        fontFamily: fonts.mono,
                        fontSize: 18,
                        fontWeight: 700,
                        color: colors.accentInk,
                        background: colors.accent,
                        padding: "4px 12px",
                        borderRadius: radius.sm,
                        letterSpacing: "0.04em",
                      }}
                    >
                      {stage.badge}
                    </span>
                  )}
                </div>

                <div
                  style={{
                    fontFamily: fonts.mono,
                    fontSize: 21,
                    color: colors.muted,
                    lineHeight: 1.4,
                  }}
                >
                  {stage.detail}
                </div>

                {/* Stage 4: animated grounding floor bar */}
                {stage.visual === "bar" && (
                  <div
                    style={{
                      marginTop: 14,
                      height: 10,
                      width: "100%",
                      background: colors.surface2,
                      borderRadius: 5,
                      overflow: "hidden",
                      border: `1px solid ${colors.line}`,
                    }}
                  >
                    <div
                      style={{
                        height: "100%",
                        width: `${barProgress * 100}%`,
                        background: colors.accent,
                        borderRadius: 5,
                      }}
                    />
                  </div>
                )}

                {/* Stage 5: match-reason tags */}
                {stage.visual === "tag" && (
                  <div
                    style={{
                      marginTop: 14,
                      display: "flex",
                      gap: 12,
                      opacity: checkOpacity,
                      flexWrap: "wrap",
                    }}
                  >
                    {["exact+hybrid", "lexical+semantic", "semantic"].map((t, idx) => (
                      <span
                        key={t}
                        style={{
                          fontFamily: fonts.mono,
                          fontSize: 17,
                          fontWeight: 700,
                          color: idx === 0 ? colors.accent : colors.muted,
                          background: idx === 0 ? colors.accentSoft : colors.surface2,
                          border: `1.5px solid ${idx === 0 ? colors.accent : colors.line}`,
                          padding: "4px 14px",
                          borderRadius: radius.sm,
                          letterSpacing: "0.05em",
                        }}
                      >
                        {t}
                      </span>
                    ))}
                  </div>
                )}
              </div>

              {/* Checkmark */}
              <div
                style={{
                  opacity: checkOpacity,
                  fontFamily: fonts.mono,
                  fontSize: 32,
                  fontWeight: 700,
                  color: colors.accent,
                  flexShrink: 0,
                  alignSelf: "center",
                }}
              >
                ✓
              </div>
            </div>
          );
        })}
      </AbsoluteFill>
    </AbsoluteFill>
  );
}