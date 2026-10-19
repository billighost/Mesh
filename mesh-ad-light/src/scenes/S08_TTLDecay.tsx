import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts, radius } from "../lib/tokens";
import MemoryCard from "../components/MemoryCard";
import GridField from "../components/GridField";
import Kicker from "../components/Kicker";

/**
 * Scene 8 — TTL Confidence Decay (frames 0–360)
 * Memory card for a staging IP; the confidence bar drains from lime to grey.
 */
export default function S08_TTLDecay() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const kickerOpacity = interpolate(frame, [0, 24], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Card entrance
  const cardSpring = spring({
    frame: frame - 20,
    fps,
    config: { damping: 22, stiffness: 120, mass: 0.7 },
  });
  const cardOpacity = interpolate(cardSpring, [0, 1], [0, 1]);
  const cardScale = interpolate(cardSpring, [0, 1], [0.92, 1]);

  // Decay bar: starts at frame 80, drains to 0 by frame 280
  const decayProgress = interpolate(frame, [80, 280], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Bar color transitions: lime → amber → muted
  const barColor =
    decayProgress > 0.6
      ? colors.accent
      : decayProgress > 0.3
      ? colors.warn
      : colors.muted;

  // Card dims as it decays
  const cardDimOpacity = interpolate(frame, [80, 280], [1, 0.6], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // "stale" label appears
  const staleOpacity = interpolate(frame, [240, 270], [0, 1], {
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
        <Kicker>TTL Confidence Decay</Kicker>
      </div>

      {/* Central card + decay bar */}
      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 0,
        }}
      >
        {/* TTL label */}
        <div
          style={{
            opacity: cardOpacity,
            fontFamily: fonts.mono,
            fontSize: 18,
            color: colors.muted,
            letterSpacing: "0.06em",
            marginBottom: 20,
            display: "flex",
            alignItems: "center",
            gap: 12,
          }}
        >
          <span style={{ fontWeight: 600 }}>ttl_days: 30</span>
          <span style={{ color: colors.line }}>·</span>
          <span>confidence decays toward staleness</span>
        </div>

        {/* Card */}
        <div
          style={{
            opacity: cardDimOpacity,
            transform: `scale(${cardScale})`,
          }}
        >
          <MemoryCard
            content="Staging server: 192.168.1.4:8080"
            memoryType="fact"
            source="deploy-agent"
            tags={[{ label: "staging" }, { label: "ip" }, { label: "temp" }]}
            entranceFrame={20}
            slideFrom="none"
            width={680}
          />
        </div>

        {/* Decay bar */}
        <div
          style={{
            marginTop: 20,
            width: 680,
            opacity: cardOpacity,
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              fontFamily: fonts.mono,
              fontSize: 16,
              fontWeight: 600,
              color: colors.muted,
              letterSpacing: "0.1em",
              textTransform: "uppercase",
              marginBottom: 8,
            }}
          >
            <span>confidence</span>
            <span style={{ color: barColor }}>
              {(decayProgress * 1.0).toFixed(2)}
            </span>
          </div>
          <div
            style={{
              height: 10,
              width: "100%",
              background: colors.surface2,
              borderRadius: radius.sm,
              overflow: "hidden",
              border: `1px solid ${colors.line}`,
            }}
          >
            <div
              style={{
                height: "100%",
                width: `${decayProgress * 100}%`,
                background: barColor,
                borderRadius: radius.sm,
                transition: "background 0.3s",
              }}
            />
          </div>

          {/* Stale label */}
          <div
            style={{
              marginTop: 12,
              opacity: staleOpacity,
              fontFamily: fonts.mono,
              fontSize: 18,
              fontWeight: 600,
              color: colors.warn,
              letterSpacing: "0.08em",
              textTransform: "uppercase",
            }}
          >
            ⚠ stale — effective confidence below 0.3
          </div>
        </div>
      </AbsoluteFill>

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
          Ephemeral facts decay.{" "}
          <span style={{ color: colors.muted }}>Permanent truths stay.</span>
        </span>
      </div>
    </AbsoluteFill>
  );
}
