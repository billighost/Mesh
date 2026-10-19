import { Composition } from "remotion";
import MeshAd, { TOTAL_FRAMES } from "./compositions/MeshAd";
import { FPS } from "./lib/tokens";

export function RemotionRoot() {
  return (
    <>
      <Composition
        id="MeshAd"
        component={MeshAd}
        durationInFrames={TOTAL_FRAMES}
        fps={FPS}
        width={1920}
        height={1080}
        defaultProps={{}}
      />
    </>
  );
}
