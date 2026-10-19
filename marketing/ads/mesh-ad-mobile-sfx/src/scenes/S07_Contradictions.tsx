import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import { colors, fonts, MOBILE, radius } from "../lib/tokens";
import MemoryCard from "../components/MemoryCard";
import GridField from "../components/GridField";
import Kicker from "../components/Kicker";

/**
 * Scene 7 — Contradiction Tracking (frames 0–360)
 * Two conflicting cards appear stacked vertically, amber flash at 100, resolved at 220.
 */
export default function S07_Contradictions() {
  const frame = useCurrentFrame();

  const kickerOpacity = interpolate(frame, [0, 24], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Conflict flash at frame 100 (synced with warn_buzz.wav at global 3580)
  const conflictFlash = interpolate(
    frame,
    [100, 108, 124, 136],
    [0, 0.28, 0.28, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );

  // Resolution state at frame 220 (synced with resolved_bell.wav at global 3700)
  const isResolved = frame > 220;

  const captionOpacity = interpolate(frame, [250, 285], [0, 1], {
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
        <Kicker>mesh_contradictions</Kicker>
        <span
          style={{
            fontFamily: fonts.mono,
            fontSize: 20,
            fontWeight: 700,
            color: isResolved ? colors.accent : frame >= 100 ? colors.warn : colors.muted,
            letterSpacing: "0.06em",
          }}
        >
          {isResolved ? "✓ CONFLICT RESOLVED" : frame >= 100 ? "⚠ 1 UNRESOLVED" : "SCANNING..."}
        </span>
      </div>

      {/* Amber conflict flash overlay */}
      <AbsoluteFill
        style={{
          background: colors.warn,
          opacity: conflictFlash,
          pointerEvents: "none",
        }}
      />

      {/* Vertical Stack: Card A → Status Divider → Card B */}
      <AbsoluteFill
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "flex-start",
          padding: `${MOBILE.safeTop + 95}px ${MOBILE.padX}px ${MOBILE.safeBottom}px`,
          gap: 32,
        }}
      >
        {/* Card A — Older conflicting memory */}
        <div
          style={{
            width: "100%",
            opacity: isResolved ? 0.55 : 1,
            transition: "opacity 0.25s",
          }}
        >
          <div
            style={{
              fontFamily: fonts.mono,
              fontSize: 19,
              fontWeight: 700,
              color: colors.muted,
              letterSpacing: "0.1em",
              textTransform: "uppercase",
              marginBottom: 12,
              display: "flex",
              justifyContent: "space-between",
            }}
          >
            <span>existing_content • agent-A</span>
            {isResolved && <span style={{ color: colors.muted }}>superseded</span>}
          </div>
          <MemoryCard
            content="Auth service requires JWT HS256 signatures"
            memoryType="instruction"
            source="agent-A"
            tags={[{ label: "auth" }, { label: "jwt" }]}
            conflict={!isResolved && frame >= 100}
            resolved={false}
            entranceFrame={20}
            slideFrom="left"
            width="100%"
          />
        </div>

        {/* Center Conflict / Resolution Badge */}
        <div
          style={{
            opacity: interpolate(frame, [80, 100], [0, 1], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            }),
            fontFamily: fonts.mono,
            fontSize: 22,
            fontWeight: 700,
            color: isResolved ? colors.accentInk : colors.warn,
            background: isResolved ? colors.accent : colors.warnSoft,
            border: `2px solid ${isResolved ? colors.accent : colors.warn}`,
            padding: "12px 28px",
            borderRadius: radius.sm,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            boxShadow: "4px 4px 0px #000",
          }}
        >
          {isResolved
            ? "✓ Resolved via mesh_resolve_contradiction"
            : "⚡ Contradiction Logged — Both Preserved"}
        </div>

        {/* Card B — New authoritative memory */}
        <div style={{ width: "100%" }}>
          <div
            style={{
              fontFamily: fonts.mono,
              fontSize: 19,
              fontWeight: 700,
              color: isResolved ? colors.accent : colors.muted,
              letterSpacing: "0.1em",
              textTransform: "uppercase",
              marginBottom: 12,
              display: "flex",
              justifyContent: "space-between",
            }}
          >
            <span>new_content • agent-B</span>
            {isResolved && <span>active truth</span>}
          </div>
          <MemoryCard
            content="Auth service requires JWT RS256 signatures with 2048-bit keys"
            memoryType="instruction"
            source="agent-B"
            tags={[
              { label: "auth", variant: "accent" },
              { label: "jwt" },
              { label: "security", variant: "accent" },
            ]}
            conflict={!isResolved && frame >= 100}
            resolved={isResolved}
            entranceFrame={35}
            slideFrom="right"
            width="100%"
          />
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
            fontSize: 44,
            letterSpacing: "-0.03em",
            lineHeight: 1.18,
            color: colors.ink,
          }}
        >
          Smart contradiction detection.{" "}
          <span style={{ color: colors.accent }}>Zero silent data loss.</span>
        </div>
      </div>
    </AbsoluteFill>
  );
}