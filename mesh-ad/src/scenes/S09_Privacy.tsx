import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts, radius } from "../lib/tokens";
import MemoryCard from "../components/MemoryCard";
import GridField from "../components/GridField";
import Kicker from "../components/Kicker";

/**
 * Scene 9 — Privacy & Security (frames 0–360)
 * local_only memory card, shield wraps it, export bounces, credential redaction.
 */
export default function S09_Privacy() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const kickerOpacity = interpolate(frame, [0, 24], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Shield draws around card: frame 60–110
  const shieldProgress = interpolate(frame, [60, 110], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Export arrow fires: frame 130–160
  const arrowX = interpolate(frame, [130, 158], [820, 920], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const arrowOpacity = interpolate(frame, [130, 132, 155, 162], [0, 0.9, 0.9, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  // Bounce × appears
  const bounceOpacity = interpolate(frame, [158, 165, 185, 200], [0, 1, 1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Credential redaction: frame 220 — text switches
  const credFrame = 220;
  const showRedacted = frame > credFrame;
  const redactOpacity = interpolate(frame, [credFrame, credFrame + 15], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Caption
  const captionOpacity = interpolate(frame, [290, 320], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill style={{ background: colors.bg }}>
      <GridField opacity={0.25} mask="center" />

      <div style={{ position: "absolute", top: 64, left: 80, opacity: kickerOpacity }}>
        <Kicker>Privacy &amp; Security</Kicker>
      </div>

      {/* Main layout */}
      <AbsoluteFill
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 60,
          paddingTop: 40,
        }}
      >
        {/* Left: local_only memory card + shield */}
        <div style={{ position: "relative" }}>
          {/* Shield SVG drawn around card */}
          <svg
            style={{
              position: "absolute",
              inset: -20,
              pointerEvents: "none",
              overflow: "visible",
            }}
            width={560 + 40}
            height={240 + 40}
            viewBox={`-20 -20 ${560 + 40} ${240 + 40}`}
          >
            <rect
              x={0}
              y={0}
              width={560}
              height={240}
              rx={4}
              fill="none"
              stroke={colors.accent}
              strokeWidth={3}
              strokeDasharray={`${(560 * 2 + 240 * 2) * shieldProgress} 9999`}
              opacity={shieldProgress > 0 ? 0.8 : 0}
            />
          </svg>

          <MemoryCard
            content="OPENAI_API_KEY=sk-••••••••••••"
            memoryType="fact"
            source="backend-agent"
            tags={[{ label: "local_only", variant: "accent" }, { label: "private" }]}
            entranceFrame={20}
            slideFrom="bottom"
            width={560}
          />

          {/* local_only badge */}
          <div
            style={{
              position: "absolute",
              top: -16,
              right: 16,
              fontFamily: fonts.mono,
              fontSize: 14,
              fontWeight: 700,
              letterSpacing: "0.08em",
              textTransform: "uppercase",
              color: colors.accentInk,
              background: colors.accent,
              padding: "4px 12px",
              borderRadius: radius.sm,
              opacity: shieldProgress,
            }}
          >
            local_only
          </div>
        </div>

        {/* Right: what happens to export attempts */}
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 24,
            width: 520,
          }}
        >
          {/* Export blocked */}
          <div
            style={{
              background: colors.surface,
              border: `1px solid ${colors.line}`,
              borderRadius: radius.sm,
              padding: "20px 26px",
              fontFamily: fonts.mono,
              fontSize: 18,
              color: colors.muted,
              boxShadow: "4px 4px 0 #000",
              position: "relative",
              overflow: "hidden",
              opacity: interpolate(frame, [60, 80], [0, 1], {
                extrapolateLeft: "clamp",
                extrapolateRight: "clamp",
              }),
            }}
          >
            <span style={{ color: colors.terminalDim }}>$ </span>
            <span style={{ color: colors.ink, fontWeight: 600 }}>mesh export backup.json</span>
            <br />
            <span style={{ color: colors.muted, fontSize: 16, marginTop: 6, display: "inline-block" }}>
              skipped 1 private memory · local_only
            </span>
          </div>

          {/* Credential redaction demo */}
          <div
            style={{
              background: colors.surface,
              border: `1px solid ${colors.line}`,
              borderRadius: radius.sm,
              padding: "20px 26px",
              fontFamily: fonts.mono,
              fontSize: 18,
              boxShadow: "4px 4px 0 #000",
              opacity: interpolate(frame, [180, 210], [0, 1], {
                extrapolateLeft: "clamp",
                extrapolateRight: "clamp",
              }),
            }}
          >
            <div style={{ color: colors.muted, fontSize: 14, fontWeight: 600, letterSpacing: "0.08em", textTransform: "uppercase", marginBottom: 8 }}>
              Auto-redaction
            </div>
            <div style={{ color: colors.terminalDim }}>
              {showRedacted ? (
                <span style={{ opacity: redactOpacity }}>
                  <span style={{ color: colors.accent, fontWeight: 600 }}>API_KEY=</span>
                  <span style={{ color: colors.warn, fontWeight: 600 }}>sk-••••••</span>
                  {"  "}
                  <span
                    style={{
                      fontSize: 13,
                      fontWeight: 600,
                      color: colors.accent,
                      border: `1px solid ${colors.accent}`,
                      padding: "2px 8px",
                      borderRadius: radius.sm,
                    }}
                  >
                    forced private
                  </span>
                </span>
              ) : (
                <span style={{ color: colors.ink }}>API_KEY=sk-abc123xyz789…</span>
              )}
            </div>
          </div>
        </div>
      </AbsoluteFill>

      {/* Export arrow bouncing off shield */}
      <svg
        style={{ position: "absolute", inset: 0, width: "100%", height: "100%", pointerEvents: "none" }}
      >
        {arrowOpacity > 0 && (
          <g opacity={arrowOpacity}>
            <text x={arrowX} y={480} textAnchor="middle" fill={colors.muted} fontSize={28} fontFamily={fonts.mono}>
              →
            </text>
          </g>
        )}
        {bounceOpacity > 0 && (
          <g opacity={bounceOpacity}>
            <text x={900} y={480} textAnchor="middle" fill={colors.warn} fontSize={32} fontFamily={fonts.mono} fontWeight={700}>
              ×
            </text>
          </g>
        )}
      </svg>

      {/* Caption */}
      <div
        style={{
          position: "absolute",
          bottom: 90,
          left: 0,
          right: 0,
          textAlign: "center",
          opacity: captionOpacity,
        }}
      >
        <span
          style={{
            fontFamily: fonts.display,
            fontWeight: 700,
            fontSize: 40,
            letterSpacing: "-0.03em",
            color: colors.ink,
          }}
        >
          Secrets stay secret.{" "}
          <span style={{ color: colors.muted }}>Always.</span>
        </span>
      </div>
    </AbsoluteFill>
  );
}
