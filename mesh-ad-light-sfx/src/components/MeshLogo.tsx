import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import { colors } from "../lib/tokens";
import MeshMark from "./MeshMark";

interface MeshLogoProps {
  size?: number;
  /** Frame to start the entrance animation */
  startFrame?: number;
  markColor?: string;
  wordmarkColor?: string;
  style?: React.CSSProperties;
}

// Approved Martian Mono SemiExpanded Bold outline path from landing/site/public/assets/brand/mesh-logo-light.svg
const WORDMARK_OUTLINE_PATH =
  "M47 615H202V551H293L229 504Q231 567 257.5 600.5Q284 634 330 634Q373 634 398.5 600.0Q424 566 427 501L372 549H512L447 504Q449 567 476.5 600.5Q504 634 551 634Q601 634 628.5 591.5Q656 549 656 471V0H512V409Q512 437 500.5 451.5Q489 466 468 466Q446 466 434.5 451.5Q423 437 423 409V0H280V409Q280 437 268.5 451.5Q257 466 235 466Q214 466 202.5 451.5Q191 437 191 409V0H47Z M899 372H1240L1187 312Q1187 403 1155.5 449.0Q1124 495 1060 495Q995 495 961.5 446.5Q928 398 928 311Q928 217 961.0 168.5Q994 120 1064 120Q1104 120 1134.5 138.5Q1165 157 1187 191H1343Q1323 124 1283.0 77.0Q1243 30 1186.5 5.5Q1130 -19 1060 -19Q970 -19 905.5 20.0Q841 59 806.5 133.0Q772 207 772 311Q772 410 807.5 482.5Q843 555 908.0 594.5Q973 634 1060 634Q1145 634 1205.0 596.0Q1265 558 1297.5 486.5Q1330 415 1330 312Q1330 296 1328.5 282.0Q1327 268 1326 256H899Z M1643 191Q1657 157 1686.5 138.5Q1716 120 1762 120Q1811 120 1837.0 134.5Q1863 149 1863 177Q1863 200 1844.5 212.5Q1826 225 1787 230L1698 240Q1594 252 1543.5 298.5Q1493 345 1493 429Q1493 524 1562.5 579.0Q1632 634 1755 634Q1885 634 1956.0 577.5Q2027 521 2033 411H1882Q1871 452 1839.0 473.5Q1807 495 1754 495Q1702 495 1675.0 480.0Q1648 465 1648 436Q1648 414 1666.0 401.5Q1684 389 1723 384L1812 374Q1917 362 1967.5 316.0Q2018 270 2018 186Q2018 122 1987.5 76.0Q1957 30 1899.5 5.5Q1842 -19 1761 -19Q1637 -19 1568.0 34.5Q1499 88 1491 191Z M2189 0V848H2351V559H2388Q2408 594 2444.0 614.0Q2480 634 2527 634Q2588 634 2631.0 605.0Q2674 576 2697.0 521.5Q2720 467 2720 390V0H2558V350Q2558 416 2532.0 449.5Q2506 483 2455 483Q2403 483 2377.0 449.5Q2351 416 2351 350V0Z";

export default function MeshLogo({
  size = 64,
  startFrame = 0,
  markColor = colors.ink,
  wordmarkColor = colors.ink,
  style,
}: MeshLogoProps) {
  const frame = useCurrentFrame();
  const elapsed = Math.max(0, frame - startFrame);

  const wordmarkOpacity = interpolate(elapsed, [38, 54], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const wordmarkX = interpolate(elapsed, [38, 54], [-12, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const wordmarkWidth = size * (135 / 64);

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: size * 0.25,
        ...style,
      }}
    >
      <MeshMark
        size={size}
        color={markColor}
        animate
        startFrame={startFrame}
      />
      <div
        style={{
          opacity: wordmarkOpacity,
          transform: `translateX(${wordmarkX}px)`,
          display: "flex",
          alignItems: "center",
        }}
      >
        <svg
          width={wordmarkWidth}
          height={size}
          viewBox="80 0 135 64"
          role="img"
          aria-label="mesh"
          style={{ display: "block" }}
        >
          <path
            d={WORDMARK_OUTLINE_PATH}
            transform="translate(80.0 46.0) scale(0.04600 -0.04600)"
            fill={wordmarkColor}
          />
        </svg>
      </div>
    </div>
  );
}
