import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { colors, fonts } from "../lib/tokens";
import AgentChip from "../components/AgentChip";
import NetworkLine from "../components/NetworkLine";
import GridField from "../components/GridField";
import Kicker from "../components/Kicker";
import MeshMark from "../components/MeshMark";

const agents = [
  { name: "backend-agent",  x: 960,  y: 240,  angle: 0 },
  { name: "frontend-agent", x: 1420, y: 480,  angle: 1 },
  { name: "review-agent",   x: 960,  y: 720,  angle: 2 },
  { name: "deploy-agent",   x: 500,  y: 480,  angle: 3 },
];

// Center (Mesh hub)
const HUB = { x: 960, y: 480 };

/**
 * Scene 6 — Multi-Agent Network (frames 0–600)
 * 4 agent chips around a central Mesh mark hub; lines draw out; packets fly.
 */
export default function S06_MultiAgent() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const kickerOpacity = interpolate(frame, [0, 24], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Hub mark entrance
  const hubSpring = spring({
    frame: frame - 10,
    fps,
    config: { damping: 20, stiffness: 120, mass: 0.7 },
  });
  const hubScale = interpolate(hubSpring, [0, 1], [0.5, 1]);
  const hubOpacity = interpolate(hubSpring, [0, 1], [0, 1]);

  // Counter text
  const counterStart = 280;
  const counterOpacity = interpolate(frame, [counterStart, counterStart + 30], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const memCount = Math.min(4, Math.floor(
    interpolate(frame, [counterStart + 10, counterStart + 80], [0, 4], {
      extrapolateLeft: "clamp", extrapolateRight: "clamp"
    })
  ));

  // Caption
  const captionOpacity = interpolate(frame, [400, 440], [0, 1], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill style={{ background: colors.bg }}>
      <GridField opacity={0.2} mask="center" />

      <div style={{ position: "absolute", top: 64, left: 80, opacity: kickerOpacity }}>
        <Kicker>Shared Agent Memory</Kicker>
      </div>

      {/* SVG network */}
      <svg
        style={{ position: "absolute", inset: 0, width: "100%", height: "100%", pointerEvents: "none" }}
      >
        {agents.map((agent, i) => (
          <NetworkLine
            key={i}
            x1={HUB.x}
            y1={HUB.y}
            x2={agent.x}
            y2={agent.y}
            startFrame={50 + i * 20}
            duration={25}
            color={colors.accent}
            strokeWidth={2.5}
            showPacket
            packetDelay={i * 15}
          />
        ))}
      </svg>

      {/* Hub — Mesh mark at center */}
      <div
        style={{
          position: "absolute",
          left: HUB.x - 60,
          top: HUB.y - 60,
          width: 120,
          height: 120,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          opacity: hubOpacity,
          transform: `scale(${hubScale})`,
        }}
      >
        <MeshMark size={96} color={colors.accent} animate startFrame={10} />
      </div>

      {/* Agent chips */}
      {agents.map((agent, i) => (
        <div
          key={i}
          style={{
            position: "absolute",
            left: agent.x - 110,
            top: agent.y - 24,
          }}
        >
          <AgentChip
            name={agent.name}
            active={frame > 160 + i * 20}
            entranceFrame={20 + i * 14}
          />
        </div>
      ))}

      {/* Counter */}
      <div
        style={{
          position: "absolute",
          top: 64,
          right: 80,
          opacity: counterOpacity,
          textAlign: "right",
          fontFamily: fonts.mono,
          fontSize: 20,
          color: colors.muted,
          letterSpacing: "0.04em",
        }}
      >
        <span style={{ color: colors.accent, fontSize: 36, fontFamily: fonts.display, fontWeight: 700 }}>
          1
        </span>{" "}
        memory stored →{" "}
        <span style={{ color: colors.accent, fontSize: 36, fontFamily: fonts.display, fontWeight: 700 }}>
          {memCount}
        </span>{" "}
        agents informed
      </div>

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
            letterSpacing: "-0.035em",
            color: colors.ink,
          }}
        >
          When one agent learns,{" "}
          <span style={{ color: colors.accent }}>everyone knows.</span>
        </span>
      </div>
    </AbsoluteFill>
  );
}
