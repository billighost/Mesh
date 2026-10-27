import { useCallback, useEffect, useRef } from "react";
import { usePreferences } from "../../hooks/usePreferences";
import {
  WORDMARK_CARET,
  WORDMARK_LETTERS,
  WORDMARK_VIEWBOX,
} from "../../data/wordmark";

const VIEWBOX_WIDTH = 2554.46;

/**
 * The footer wordmark: an outlined ghost of "mesh" that becomes a small
 * coordinate field under the cursor.
 *
 * The outlines are real paths, not text with -webkit-text-stroke: Martian
 * Mono's glyphs are built from overlapping contours and a text stroke draws
 * those construction seams as stray internal lines. The paths below were
 * generated from the font and unioned, so each letter is one clean edge.
 *
 * Two layers share one pointer sample:
 *  - an acid-lime ink layer, revealed through a radial mask;
 *  - an SVG lattice linking the cursor to the two nearest letter anchors.
 * The nearest letters also lean in a couple of pixels. Everything is
 * direct DOM writes inside a rAF, so the footer never re-renders.
 */
export default function FooterWordmark() {
  const { motion } = usePreferences();
  const wordRef = useRef(null);
  const outlineRef = useRef(null);
  const inkRef = useRef(null);
  const linesRef = useRef(null);
  const lineRefs = useRef([]);
  const dotRefs = useRef([]);
  const cursorDotRef = useRef(null);
  const anchorsRef = useRef([]);
  const letterRefs = useRef([]);
  const pointRef = useRef(null);
  const displayRef = useRef(null);
  const fontSizeRef = useRef(96);
  const unitsPerPxRef = useRef(1);
  const frameRef = useRef(0);
  const tickRef = useRef(null);

  const reset = useCallback(() => {
    const word = wordRef.current;
    if (!word) return;
    if (frameRef.current) cancelAnimationFrame(frameRef.current);
    frameRef.current = 0;
    pointRef.current = null;
    displayRef.current = null;
    word.classList.remove("is-live");
    const ink = inkRef.current;
    if (ink) {
      ink.style.setProperty("--mx", "-999px");
      ink.style.setProperty("--my", "-999px");
    }
    word.querySelectorAll(".footer__word-letter").forEach((letter) => {
      letter.style.transform = "";
    });
    dotRefs.current.forEach((dot) => dot && dot.classList.remove("is-near"));
  }, []);

  /* Measure once per layout: anchors are read untransformed. */
  useEffect(() => {
    const word = wordRef.current;
    const lines = linesRef.current;

    const measure = () => {
      if (!word || !lines) return;
      const rect = word.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      lines.setAttribute("viewBox", `0 0 ${rect.width} ${rect.height}`);

      const outlines = Array.from(
        word.querySelectorAll('.footer__word-letter[data-layer="outline"]')
      );
      const anchors = outlines.map((letter) => {
        const box = letter.getBoundingClientRect();
        return {
          x: box.left - rect.left + box.width / 2,
          y: box.top - rect.top + box.height / 2,
        };
      });
      anchorsRef.current = anchors;
      letterRefs.current = WORDMARK_LETTERS.map((_, index) =>
        Array.from(word.querySelectorAll(`.footer__word-letter[data-index="${index}"]`))
      );

      anchors.forEach((anchor, index) => {
        const dot = dotRefs.current[index];
        if (!dot) return;
        dot.setAttribute("cx", anchor.x.toFixed(2));
        dot.setAttribute("cy", anchor.y.toFixed(2));
      });

      /* Pointer math and SVG user units both depend on these. */
      fontSizeRef.current = parseFloat(window.getComputedStyle(word).fontSize) || 96;
      const outline = outlineRef.current;
      const rendered = outline ? outline.getBoundingClientRect().width : 0;
      unitsPerPxRef.current = rendered ? VIEWBOX_WIDTH / rendered : 1;
    };

    measure();
    const raf = requestAnimationFrame(measure);
    let observer = null;
    if (word && typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(measure);
      observer.observe(word);
    }

    return () => {
      cancelAnimationFrame(raf);
      if (observer) observer.disconnect();
    };
  }, []);

  useEffect(() => {
    if (!motion) reset();
  }, [motion, reset]);

  /* Paint one frame at a word-local pointer position. */
  const paint = useCallback((cx, cy) => {
    const word = wordRef.current;
    if (!word) return;

    const rect = word.getBoundingClientRect();
    const x = cx - rect.left;
    const y = cy - rect.top;

    /* The mask lives on the ink SVG, whose box is inset from the word. */
    const ink = inkRef.current;
    if (ink) {
      const inkRect = ink.getBoundingClientRect();
      ink.style.setProperty("--mx", `${(cx - inkRect.left).toFixed(1)}px`);
      ink.style.setProperty("--my", `${(cy - inkRect.top).toFixed(1)}px`);
    }

    const anchors = anchorsRef.current;
    if (!anchors.length) return;

    const ranked = anchors
      .map((anchor, index) => ({
        index,
        distance: Math.hypot(anchor.x - x, anchor.y - y),
      }))
      .sort((a, b) => a.distance - b.distance);
    const near = [ranked[0].index, ranked[1].index];

    near.forEach((index, slot) => {
      const line = lineRefs.current[slot];
      const anchor = anchors[index];
      if (!line || !anchor) return;
      line.setAttribute("x1", anchor.x.toFixed(2));
      line.setAttribute("y1", anchor.y.toFixed(2));
      line.setAttribute("x2", x.toFixed(2));
      line.setAttribute("y2", y.toFixed(2));
    });

    dotRefs.current.forEach((dot, index) => {
      if (dot) dot.classList.toggle("is-near", near.includes(index));
    });

    if (cursorDotRef.current) {
      cursorDotRef.current.setAttribute("cx", x.toFixed(2));
      cursorDotRef.current.setAttribute("cy", y.toFixed(2));
    }

    /*
     * Group transforms are in SVG user units, while the pointer is in CSS
     * pixels, so the pull is converted by the rendered-width ratio.
     */
    const fontSize = fontSizeRef.current;
    const unitsPerPx = unitsPerPxRef.current || 1;

    anchors.forEach((anchor, index) => {
      const dx = x - anchor.x;
      const dy = y - anchor.y;
      const distance = Math.hypot(dx, dy) || 1;
      const strength = Math.max(0, 1 - distance / (fontSize * 1.05));
      const shift = fontSize * 0.018 * strength * strength * unitsPerPx;
      const transform =
        shift > 0.5
          ? `translate(${((dx / distance) * shift).toFixed(2)}px, ${((dy / distance) * shift).toFixed(2)}px)`
          : "";
      const letters = letterRefs.current[index];
      if (letters) letters.forEach((letter) => { letter.style.transform = transform; });
    });
  }, []);

  /*
   * The ink and the lattice trail the pointer with a short exponential ease
   * instead of snapping to it. The loop parks itself once it has caught up,
   * so an idle footer costs nothing.
   */
  const tick = useCallback(() => {
    frameRef.current = 0;
    const display = displayRef.current;
    const target = pointRef.current;
    if (!display || !target) return;

    const dx = target.x - display.x;
    const dy = target.y - display.y;

    if (Math.abs(dx) < 0.35 && Math.abs(dy) < 0.35) {
      display.x = target.x;
      display.y = target.y;
      paint(target.x, target.y);
      return;
    }

    display.x += dx * 0.16;
    display.y += dy * 0.16;
    paint(display.x, display.y);
    frameRef.current = requestAnimationFrame(tickRef.current);
  }, [paint]);

  useEffect(() => {
    tickRef.current = tick;
  }, [tick]);

  const handlePointerEnter = (event) => {
    if (!motion) return;
    const word = wordRef.current;
    if (!word) return;
    word.classList.add("is-live");
    pointRef.current = { x: event.clientX, y: event.clientY };
    displayRef.current = { x: event.clientX, y: event.clientY };
    paint(event.clientX, event.clientY);
  };

  const handlePointerMove = (event) => {
    if (!motion) return;
    pointRef.current = { x: event.clientX, y: event.clientY };
    if (!frameRef.current) frameRef.current = requestAnimationFrame(tickRef.current);
  };

  const handlePointerLeave = () => {
    reset();
  };

  const handleTouchStart = (event) => {
    if (!motion) return;
    const touch = event.touches[0];
    if (!touch) return;
    const word = wordRef.current;
    if (!word) return;
    word.classList.add("is-live");
    pointRef.current = { x: touch.clientX, y: touch.clientY };
    displayRef.current = { x: touch.clientX, y: touch.clientY };
    paint(touch.clientX, touch.clientY);
  };

  const handleTouchMove = (event) => {
    if (!motion) return;
    const touch = event.touches[0];
    if (!touch) return;
    pointRef.current = { x: touch.clientX, y: touch.clientY };
    if (!frameRef.current) frameRef.current = requestAnimationFrame(tickRef.current);
  };

  const handleTouchEnd = () => {
    reset();
  };

  return (
    <div
      className="footer__word"
      ref={wordRef}
      onPointerEnter={handlePointerEnter}
      onPointerMove={handlePointerMove}
      onPointerLeave={handlePointerLeave}
      onTouchStart={handleTouchStart}
      onTouchMove={handleTouchMove}
      onTouchEnd={handleTouchEnd}
      aria-hidden="true"
    >
      <div className="footer__word-stage">
        <svg
          className="footer__word-svg footer__word-svg--outline"
          ref={outlineRef}
          viewBox={WORDMARK_VIEWBOX}
          focusable="false"
        >
          {WORDMARK_LETTERS.map((letter, index) => (
            <g
              className="footer__word-letter"
              data-index={index}
              data-layer="outline"
              key={letter.ch}
            >
              <path d={letter.d} />
            </g>
          ))}
          <rect
            className="footer__word-caret"
            x={WORDMARK_CARET.x}
            y={WORDMARK_CARET.y}
            width={WORDMARK_CARET.width}
            height={WORDMARK_CARET.height}
          />
        </svg>

        <svg
          className="footer__word-svg footer__word-svg--ink"
          ref={inkRef}
          viewBox={WORDMARK_VIEWBOX}
          focusable="false"
        >
          {WORDMARK_LETTERS.map((letter, index) => (
            <g
              className="footer__word-letter"
              data-index={index}
              data-layer="ink"
              key={letter.ch}
            >
              <path d={letter.d} />
            </g>
          ))}
        </svg>
      </div>

      <svg
        className="footer__word-lines"
        ref={linesRef}
        aria-hidden="true"
        focusable="false"
      >
        {WORDMARK_LETTERS.map((letter, index) => (
          <circle
            className="footer__word-anchor"
            key={`anchor-${letter.ch}`}
            r="2.4"
            ref={(node) => { dotRefs.current[index] = node; }}
          />
        ))}
        {[0, 1].map((index) => (
          <line
            className="footer__word-line"
            key={`line-${index}`}
            ref={(node) => { lineRefs.current[index] = node; }}
          />
        ))}
        <circle className="footer__word-cursor" r="3.2" ref={cursorDotRef} />
      </svg>
    </div>
  );
}
