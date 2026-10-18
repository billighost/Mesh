import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts } from "../lib/tokens";
import MemoryCard from "../components/MemoryCard";
import GridField from "../components/GridField";
import Kicker from "../components/Kicker";

/**
 * Scene 7 — Contradiction Tracking (frames 0–360)
 * Two conflicting cards appear, amber flash, then resolved.
 */
export default function S07_Contradictions() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const kickerOpacity = interpolate(frame, [0, 24], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Conflict flash at ~frame 100
  const conflictFlash = interpolate(
    frame,
    [100, 108, 124, 132],
    [0, 0.25, 0.25, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

  // Resolution: second card gets "resolved" state at frame 220
  const isResolved = frame > 220;

  // Caption
  const captionOpacity = interpolate(frame, [260, 300], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill style={{ background: colors.bg }}>
      <GridField opacity={0.25} mask="center" />

      <div style={{ position: "absolute", top: 64, left: 80, opacity: kickerOpacity }}>
        <Kicker>mesh_contradictions</Kicker>
      </div>

      {/* Amber conflict flash overlay */}
      <AbsoluteFill
        style={{
          background: colors.warn,
          opacity: conflictFlash,
          pointerEvents: "none",
        }}
      />

      {/* Two cards side by side */}
      <AbsoluteFill
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 48,
          paddingTop: 60,
        }}
      >
        {/* Card A — conflict */}
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 12 }}>
          <span
            style={{
              fontFamily: fonts.mono,
              fontSize: 11,
              color: colors.muted,
              letterSpacing: "0.1em",
              textTransform: "uppercase",
            }}
          >
            agent-A
          </span>
          <MemoryCard
            content={"Auth service requires JWT HS256 signatures"}
            memoryType="instruction"
            source="agent-A"
            tags={[{ label: "auth" }, { label: "jwt" }]}
            conflict={!isResolved}
            resolved={false}
            entranceFrame={20}
            slideFrom="left"
            width={400}
          />
        </div>

        {/* VS divider */}
        <div
          style={{
            fontFamily: fonts.mono,
            fontSize: 20,
            color: frame > 100 ? colors.warn : colors.line,
            letterSpacing: "0.1em",
            opacity: interpolate(frame, [80, 100], [0, 1], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            }),
          }}
        >
          ⚠
        </div>

        {/* Card B — resolved */}
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 12 }}>
          <span
            style={{
              fontFamily: fonts.mono,
              fontSize: 11,
              color: colors.muted,
              letterSpacing: "0.1em",
              textTransform: "uppercase",
            }}
          >
            agent-B
          </span>
          <MemoryCard
            content={"Auth service requires JWT RS256 signatures with 2048-bit keys"}
            memoryType="instruction"
            source="agent-B"
            tags={[{ label: "auth", variant: "accent" }, { label: "jwt" }, { label: "security" }]}
            conflict={!isResolved && frame > 80}
            resolved={isResolved}
            entranceFrame={35}
            slideFrom="right"
            width={400}
          />
        </div>
      </AbsoluteFill>

      {/* Caption */}
      <div
        style={{
          position: "absolute",
          bottom: 100,
          left: 0,
          right: 0,
          textAlign: "center",
          opacity: captionOpacity,
        }}
      >
        <span
          style={{
            fontFamily: fonts.display,
            fontWeight: 600,
            fontSize: 28,
            letterSpacing: "-0.03em",
            color: colors.ink,
          }}
        >
          Smart contradiction detection.{" "}
          <span style={{ color: colors.muted }}>No silent data loss.</span>
        </span>
      </div>
    </AbsoluteFill>
  );
}
