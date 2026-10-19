import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { colors, fonts, MOBILE, radius } from "../lib/tokens";
import MemoryCard from "../components/MemoryCard";
import GridField from "../components/GridField";
import Kicker from "../components/Kicker";

/**
 * Scene 9 — Privacy & Security (frames 0–240 in MeshAd sequence)
 * local_only memory card, shield wraps it, export deflects at frame 158, guard_ingest() redaction.
 */
export default function S09_Privacy() {
  const frame = useCurrentFrame();

  const kickerOpacity = interpolate(frame, [0, 20], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Shield draws around card: frames 35–85
  const shieldProgress = interpolate(frame, [35, 85], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Credential auto-redaction triggers at frame 115
  const credFrame = 115;
  const showRedacted = frame >= credFrame;
  const redactOpacity = interpolate(frame, [credFrame, credFrame + 15], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Export deflection at frame 158 (synced with shield_deflect.wav at global 4358)
  const deflectOpacity = interpolate(frame, [154, 160, 215, 230], [0, 1, 1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const deflectScale = interpolate(frame, [155, 164, 174], [0.85, 1.06, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Caption fades in at 165 so it stays readable through frame 240
  const captionOpacity = interpolate(frame, [165, 190], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill style={{ background: colors.bg }}>
      <GridField opacity={0.25} mask="center" />

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
        <Kicker>Privacy &amp; Security</Kicker>
        <span
          style={{
            fontFamily: fonts.mono,
            fontSize: 20,
            fontWeight: 700,
            color: colors.accent,
            letterSpacing: "0.06em",
          }}
        >
          ~/.mesh (0700)
        </span>
      </div>

      {/* Vertical Stack */}
      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "flex-start",
          padding: `${MOBILE.safeTop + 88}px ${MOBILE.padX}px ${MOBILE.safeBottom}px`,
          gap: 32,
        }}
      >
        {/* 1. Shielded Private Memory Card */}
        <div style={{ position: "relative", width: "100%" }}>
          <svg
            style={{
              position: "absolute",
              inset: -14,
              pointerEvents: "none",
              overflow: "visible",
            }}
            width={MOBILE.contentWidth + 28}
            height={250}
            viewBox={`-14 -14 ${MOBILE.contentWidth + 28} 250`}
          >
            <rect
              x={0}
              y={0}
              width={MOBILE.contentWidth}
              height={222}
              rx={8}
              fill="none"
              stroke={colors.accent}
              strokeWidth={4}
              strokeDasharray={`${(MOBILE.contentWidth * 2 + 222 * 2) * shieldProgress} 9999`}
              opacity={shieldProgress > 0 ? 0.9 : 0}
            />
          </svg>

          <MemoryCard
            content="OPENAI_API_KEY=sk-••• [REDACTED]"
            memoryType="fact"
            source="backend-agent"
            tags={[
              { label: "local_only", variant: "accent" },
              { label: "private", variant: "accent" },
            ]}
            entranceFrame={10}
            slideFrom="bottom"
            width="100%"
          />

          {/* local_only shield badge */}
          <div
            style={{
              position: "absolute",
              top: -18,
              right: 24,
              fontFamily: fonts.mono,
              fontSize: 18,
              fontWeight: 700,
              letterSpacing: "0.08em",
              textTransform: "uppercase",
              color: colors.accentInk,
              background: colors.accent,
              padding: "6px 16px",
              borderRadius: radius.sm,
              opacity: shieldProgress,
              boxShadow: "4px 4px 0px #000",
            }}
          >
            🔒 local_only=True
          </div>
        </div>

        {/* 2. Export Blocked + Shield Deflection Banner */}
        <div
          style={{
            width: "100%",
            background: colors.surface,
            border: `2px solid ${frame >= 158 ? colors.warn : colors.line}`,
            borderRadius: radius.md,
            padding: "24px 30px",
            fontFamily: fonts.mono,
            fontSize: 22,
            color: colors.muted,
            boxShadow: "6px 6px 0px #000",
            opacity: interpolate(frame, [45, 70], [0, 1], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            }),
          }}
        >
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <div>
              <span style={{ color: colors.accent, fontWeight: 700 }}>$ </span>
              <span style={{ color: colors.ink, fontWeight: 700 }}>
                mesh export backup.json
              </span>
            </div>

            {deflectOpacity > 0 && (
              <span
                style={{
                  opacity: deflectOpacity,
                  transform: `scale(${deflectScale})`,
                  color: colors.warn,
                  background: colors.warnSoft,
                  border: `1.5px solid ${colors.warn}`,
                  padding: "4px 14px",
                  borderRadius: radius.sm,
                  fontSize: 18,
                  fontWeight: 700,
                }}
              >
                ✕ DEFLECTED BY SHIELD
              </span>
            )}
          </div>
          <div style={{ color: colors.muted, fontSize: 20, marginTop: 10 }}>
            → skipped 1 private memory • excluded from MCP &amp; exports
          </div>
        </div>

        {/* 3. Ingestion Guard: Auto-Redaction & Poison Rejection */}
        <div
          style={{
            width: "100%",
            background: colors.surface,
            border: `2px solid ${showRedacted ? colors.accent : colors.line}`,
            borderRadius: radius.md,
            padding: "24px 30px",
            fontFamily: fonts.mono,
            fontSize: 22,
            boxShadow: "6px 6px 0px #000",
            opacity: interpolate(frame, [80, 105], [0, 1], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            }),
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              color: colors.muted,
              fontSize: 18,
              fontWeight: 700,
              letterSpacing: "0.08em",
              textTransform: "uppercase",
              marginBottom: 14,
            }}
          >
            <span>guard_ingest() • Auto-Redaction</span>
            <span style={{ color: colors.accent }}>✓ Prompt-Injection Guard</span>
          </div>

          <div style={{ fontSize: 24 }}>
            {showRedacted ? (
              <div
                style={{
                  opacity: redactOpacity,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  flexWrap: "wrap",
                  gap: 12,
                }}
              >
                <div>
                  <span style={{ color: colors.accent, fontWeight: 700 }}>API_KEY=</span>
                  <span style={{ color: colors.warn, fontWeight: 700 }}>sk-••• [REDACTED]</span>
                </div>
                <span
                  style={{
                    fontSize: 18,
                    fontWeight: 700,
                    color: colors.accent,
                    background: colors.accentSoft,
                    border: `1.5px solid ${colors.accent}`,
                    padding: "4px 14px",
                    borderRadius: radius.sm,
                  }}
                >
                  FORCED PRIVATE
                </span>
              </div>
            ) : (
              <span style={{ color: colors.ink }}>API_KEY=sk-abc123xyz789...</span>
            )}
          </div>
        </div>
      </AbsoluteFill>

      {/* Bottom Takeaway Caption */}
      <div
        style={{
          position: "absolute",
          bottom: 220,
          left: MOBILE.padX,
          right: MOBILE.padX,
          textAlign: "center",
          opacity: captionOpacity,
        }}
      >
        <div
          style={{
            fontFamily: fonts.display,
            fontWeight: 800,
            fontSize: 48,
            letterSpacing: "-0.03em",
            color: colors.ink,
          }}
        >
          Secrets stay secret.{" "}
          <span style={{ color: colors.accent }}>Always.</span>
        </div>
      </div>
    </AbsoluteFill>
  );
}