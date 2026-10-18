import React from "react";
import {
  AbsoluteFill,
  interpolate,
  Sequence,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { colors } from "../lib/tokens";

import S00_ColdOpen from "../scenes/S00_ColdOpen";
import S01_Problem from "../scenes/S01_Problem";
import S02_HeroReveal from "../scenes/S02_HeroReveal";
import S03_Learn from "../scenes/S03_Learn";
import S04_Recall from "../scenes/S04_Recall";
import S05_HybridSearch from "../scenes/S05_HybridSearch";
import S06_MultiAgent from "../scenes/S06_MultiAgent";
import S07_Contradictions from "../scenes/S07_Contradictions";
import S08_TTLDecay from "../scenes/S08_TTLDecay";
import S09_Privacy from "../scenes/S09_Privacy";
import S10_Interfaces from "../scenes/S10_Interfaces";
import S11_Install from "../scenes/S11_Install";
import S12_Finale from "../scenes/S12_Finale";

// ─── Scene timing at 60fps ──────────────────────────────────────────────────
// Total: ~90 seconds = 5400 frames
const SCENES = [
  // [name, durationFrames]
  ["ColdOpen",      180],  // 0s  – 3s
  ["Problem",       540],  // 3s  – 12s
  ["HeroReveal",    360],  // 12s – 18s
  ["Learn",         600],  // 18s – 28s
  ["Recall",        600],  // 28s – 38s
  ["HybridSearch",  600],  // 38s – 48s
  ["MultiAgent",    600],  // 48s – 58s
  ["Contradictions",360],  // 58s – 64s
  ["TTLDecay",      360],  // 64s – 70s
  ["Privacy",       240],  // 70s – 74s
  ["Interfaces",    360],  // 74s – 80s
  ["Install",       240],  // 80s – 84s
  ["Finale",        540],  // 84s – 93s
] as const;

// Compute cumulative start frames
function getSceneStarts() {
  const starts: number[] = [];
  let cursor = 0;
  for (const [, dur] of SCENES) {
    starts.push(cursor);
    cursor += dur;
  }
  return starts;
}
const SCENE_STARTS = getSceneStarts();
export const TOTAL_FRAMES = SCENE_STARTS[SCENE_STARTS.length - 1] + SCENES[SCENES.length - 1][1];

/** Cross-fade overlay between scenes (18 frames) */
function CrossFade({ start, duration = 18 }: { start: number; duration?: number }) {
  const frame = useCurrentFrame();
  const opacity = interpolate(
    frame,
    [start, start + duration / 2, start + duration],
    [0, 0.6, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );
  return (
    <AbsoluteFill
      style={{ background: colors.bg, opacity, pointerEvents: "none" }}
    />
  );
}

export default function MeshAd() {
  return (
    <AbsoluteFill style={{ background: colors.bg, fontFamily: "sans-serif" }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&family=Martian+Mono:wght@400;500;600;700;800&display=swap');
      `}</style>

      <Sequence from={SCENE_STARTS[0]}  durationInFrames={SCENES[0][1]}>  <S00_ColdOpen /> </Sequence>
      <Sequence from={SCENE_STARTS[1]}  durationInFrames={SCENES[1][1]}>  <S01_Problem /> </Sequence>
      <Sequence from={SCENE_STARTS[2]}  durationInFrames={SCENES[2][1]}>  <S02_HeroReveal /> </Sequence>
      <Sequence from={SCENE_STARTS[3]}  durationInFrames={SCENES[3][1]}>  <S03_Learn /> </Sequence>
      <Sequence from={SCENE_STARTS[4]}  durationInFrames={SCENES[4][1]}>  <S04_Recall /> </Sequence>
      <Sequence from={SCENE_STARTS[5]}  durationInFrames={SCENES[5][1]}>  <S05_HybridSearch /> </Sequence>
      <Sequence from={SCENE_STARTS[6]}  durationInFrames={SCENES[6][1]}>  <S06_MultiAgent /> </Sequence>
      <Sequence from={SCENE_STARTS[7]}  durationInFrames={SCENES[7][1]}>  <S07_Contradictions /> </Sequence>
      <Sequence from={SCENE_STARTS[8]}  durationInFrames={SCENES[8][1]}>  <S08_TTLDecay /> </Sequence>
      <Sequence from={SCENE_STARTS[9]}  durationInFrames={SCENES[9][1]}>  <S09_Privacy /> </Sequence>
      <Sequence from={SCENE_STARTS[10]} durationInFrames={SCENES[10][1]}> <S10_Interfaces /> </Sequence>
      <Sequence from={SCENE_STARTS[11]} durationInFrames={SCENES[11][1]}> <S11_Install /> </Sequence>
      <Sequence from={SCENE_STARTS[12]} durationInFrames={SCENES[12][1]}> <S12_Finale /> </Sequence>

      {/* Cross-fade transitions between scenes */}
      {SCENE_STARTS.slice(1).map((start) => (
        <CrossFade key={start} start={start - 9} duration={18} />
      ))}

    </AbsoluteFill>
  );
}
