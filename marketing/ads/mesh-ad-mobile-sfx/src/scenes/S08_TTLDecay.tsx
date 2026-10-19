import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts, MOBILE, radius } from "../lib/tokens";
import MemoryCard from "../components/MemoryCard";
import GridField from "../components/GridField";
import Kicker from "../components/Kicker";

/**
 * Scene 8 — TTL Confidence Decay (frames 0–360)
 * Memory card for a staging IP; the confidence bar drains from lime to amber to muted grey.
 */
export default function S08_TTLDecay() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const kickerOpacity = interpolate(frame, [0, 24], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const cardSpring = spring({
    frame: frame - 20,
    fps,
    config: { damping: 22, stiffness: 120, mass: 0.7 },
  });
  const cardOpacity = interpolate(cardSpring, [0, 1], [0, 1]);
  const cardScale = interpolate(cardSpring, [0, 1], [0.94, 1]);

  // Decay bar: starts at frame 80, drains to 0 by frame 280 (synced with decay_filter.wav at 3940 = scene frame 100)
  const decayProgress = interpolate(frame, [80, 280], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const barColor =
    decayProgress > 0.6
      ? colors.accent
      : decayProgress > 0.3
      ? colors.warn
      : colors.muted;

  const cardDimOpacity = interpolate(frame, [80, 280], [1, 0.55], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const staleOpacity = interpolate(frame, [220, 250], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const captionOpacity = interpolate(frame, [265, 300], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const elapsedDays = Math.min(30, Math.floor((1 - decayProgress) * 30));

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
        <Kicker>TTL Confidence Decay</Kicker>
        <span
          style={{
            fontFamily: fonts.mono,
            fontSize: 22,
            fontWeight: 700,
            color: colors.accent,
            letterSpacing: "0.06em",
          }}
        >
          DAY {elapsedDays} / 30
        </span>
      </div>

      {/* Central vertical stack */}
      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          padding: `0 ${MOBILE.padX}px`,
          paddingBottom: 90,
          gap: 36,
        }}
      >
        {/* Parameter pill */}
        <div
          style={{
            opacity: cardOpacity,
            width: "100%",
            background: colors.surface,
            border: `1.5px solid ${colors.line}`,
            borderRadius: radius.md,
            padding: "18px 26px",
            fontFamily: fonts.mono,
            fontSize: 22,
            color: colors.muted,
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            boxShadow: "4px 4px 0px #000",
          }}
        >
          <span>
            <strong style={{ color: colors.accent }}>ttl_days: 30</strong> • auto-decay
          </span>
          <span style={{ color: colors.ink, fontSize: 19 }}>
            include_stale=False
          </span>
        </div>

        {/* Memory Card */}
        <div
          style={{
            width: "100%",
            opacity: cardDimOpacity,
            transform: `scale(${cardScale})`,
          }}
        >
          <MemoryCard
            content="Staging server: 192.168.1.4:8080"
            memoryType="fact"
            source="deploy-agent"
            tags={[{ label: "staging" }, { label: "ip" }, { label: "temp" }]}
            conflict={decayProgress <= 0.3}
            entranceFrame={20}
            slideFrom="none"
            width="100%"
          />
        </div>

        {/* Decay Gauge Card */}
        <div
          style={{
            width: "100%",
            opacity: cardOpacity,
            background: colors.surface,
            border: `2px solid ${decayProgress <= 0.3 ? colors.warn : colors.line}`,
            borderRadius: radius.md,
            padding: "28px 32px",
            boxShadow: "6px 6px 0px #000",
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "baseline",
              fontFamily: fonts.mono,
              marginBottom: 16,
            }}
          >
            <span
              style={{
                fontSize: 22,
                fontWeight: 700,
                color: colors.ink,
                letterSpacing: "0.08em",
                textTransform: "uppercase",
              }}
            >
              Effective Confidence
            </span>
            <span
              style={{
                fontSize: 44,
                fontFamily: fonts.display,
                fontWeight: 800,
                color: barColor,
              }}
            >
              {(decayProgress * 1.0).toFixed(2)}
            </span>
          </div>

          <div
            style={{
              height: 22,
              width: "100%",
              background: colors.surface2,
              borderRadius: radius.sm,
              overflow: "hidden",
              border: `1.5px solid ${colors.line}`,
            }}
          >
            <div
              style={{
                height: "100%",
                width: `${decayProgress * 100}%`,
                background: barColor,
                borderRadius: radius.sm,
              }}
            />
          </div>

          {/* Stale threshold callout */}
          <div
            style={{
              marginTop: 20,
              opacity: staleOpacity,
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              fontFamily: fonts.mono,
              fontSize: 20,
              fontWeight: 700,
              color: colors.warn,
              background: colors.warnSoft,
              border: `1px solid ${colors.warn}`,
              padding: "12px 18px",
              borderRadius: radius.sm,
              letterSpacing: "0.05em",
            }}
          >
            <span>⚠ STALE • CONFIDENCE &lt; 0.30</span>
            <span style={{ color: colors.ink, fontSize: 17 }}>
              mesh_update(clear_ttl=True) to keep
            </span>
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
            fontSize: 46,
            letterSpacing: "-0.03em",
            lineHeight: 1.18,
            color: colors.ink,
          }}
        >
          Ephemeral facts decay.{" "}
          <span style={{ color: colors.accent }}>Permanent truths stay.</span>
        </div>
      </div>
    </AbsoluteFill>
  );
}