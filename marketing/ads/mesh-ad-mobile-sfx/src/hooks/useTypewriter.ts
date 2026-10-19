import { useCurrentFrame } from "remotion";

/**
 * Returns a substring of `text` that grows one character per `charsPerFrame`.
 */
export function useTypewriter(
  text: string,
  startFrame: number,
  charsPerFrame = 0.5
): string {
  const frame = useCurrentFrame();
  const elapsed = Math.max(0, frame - startFrame);
  const charCount = Math.min(text.length, Math.floor(elapsed * charsPerFrame));
  return text.slice(0, charCount);
}

/**
 * Returns multiple lines, each starting after the previous finishes.
 */
export function useTypewriterLines(
  lines: string[],
  startFrame: number,
  charsPerFrame = 0.8
): string[] {
  const frame = useCurrentFrame();
  const result: string[] = [];
  let cursor = startFrame;

  for (const line of lines) {
    const elapsed = Math.max(0, frame - cursor);
    const charCount = Math.min(line.length, Math.floor(elapsed * charsPerFrame));
    result.push(line.slice(0, charCount));
    // Next line starts after this one finishes + small pause (12 frames)
    cursor += Math.ceil(line.length / charsPerFrame) + 12;
    if (frame < cursor - Math.ceil(line.length / charsPerFrame)) break;
  }

  return result;
}