import React from "react";
import { Composition } from "remotion";
import MeshAd, { TOTAL_FRAMES } from "./compositions/MeshAd";
import { FPS, MOBILE } from "./lib/tokens";

export function RemotionRoot() {
  return (
    <>
      <Composition
        id="MeshAd"
        component={MeshAd}
        durationInFrames={TOTAL_FRAMES}
        fps={FPS}
        width={MOBILE.width}
        height={MOBILE.height}
        defaultProps={{}}
      />
      <Composition
        id="MeshAdMobile"
        component={MeshAd}
        durationInFrames={TOTAL_FRAMES}
        fps={FPS}
        width={MOBILE.width}
        height={MOBILE.height}
        defaultProps={{}}
      />
    </>
  );
}