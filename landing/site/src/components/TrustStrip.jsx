import { AGENSI_URL, agents } from "../data/content";
import Badge from "./ui/Badge";
import Icon from "./ui/Icon";
import Marquee from "./ui/Marquee";

export default function TrustStrip() {
  return (
    <section className="trust" aria-label="Availability and agent compatibility">
      <div className="wrap">
        <div className="trust__top">
          <a
            className="trust__label"
            href={AGENSI_URL}
            target="_blank"
            rel="noopener noreferrer"
          >
            <span className="trust__pulse presence-dot" aria-hidden="true" />
            Free on Agensi
            <Icon name="arrowUpRight" size={12} />
            <span className="visually-hidden">(opens in a new tab)</span>
          </a>
          <div className="trust__badges">
            <Badge tone="accent">Security scanned</Badge>
            <Badge>27 agents</Badge>
            <Badge>MIT</Badge>
          </div>
          <span className="trust__meta">v0.6.4 / Python 3.10+</span>
        </div>
        <div className="trust__bottom">
          <Marquee items={agents} label="Works with Claude Code, Cursor, Codex CLI, Muse, Dots, OpenClaw, Manus, Grok, Grok Bot, and 18 more agents via Agensi" />
        </div>
      </div>
    </section>
  );
}
