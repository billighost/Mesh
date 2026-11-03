import { AGENSI_URL } from "../data/content";
import Badge from "./ui/Badge";
import Button from "./ui/Button";
import SectionHeader from "./ui/SectionHeader";
import Terminal from "./ui/Terminal";


const agensiLines = [
  '$ mesh brief --max-tokens 500',
  "ok  2 open goals / 5 tasks / 1 conflict",
  '$ mesh task done task-12 --summary "auth live"',
  "ok  logged / evidence recorded / changelog updated",
];

export default function AgensiSection() {
  return (
    <section className="section" id="agensi" aria-labelledby="agensi-title">
      <div className="wrap">
        <SectionHeader
          index="07"
          route="DISTRIBUTION"
          id="agensi-title"
          title="Free on Agensi. Security scanned. 27 agents."
          lede="Mesh is published as a free, security-scanned SKILL.md skill on Agensi. Use it from Claude Code, Claude Desktop, Cursor, Windsurf, Codex, Gemini CLI, Antigravity, and 20 more — or install the package directly and configure it yourself."
          meta="listing attributes / via Agensi"
        />

        <div className="agensi__panel">
          <div className="agensi__copy">
            <h3>The skill is free. The memory is yours.</h3>
            <div className="agensi__badges">
              <Badge tone="accent">Free</Badge>
              <Badge tone="ok">Security scanned</Badge>
              <Badge>27 agents</Badge>
              <Badge>17 MCP tools</Badge>
            </div>
            <p>
              The listing includes a worked session, the full install flow, and example
              task and memory workflows. One click gets the skill; the package is the
              same local engine that runs in your terminal.
            </p>
            <div className="agensi__actions">
              <Button href={AGENSI_URL} iconRight="arrowUpRight">
                Use this skill on Agensi
              </Button>
              <Button href={AGENSI_URL} variant="quiet" iconRight="arrowUpRight">
                Read the listing
              </Button>
            </div>
            <p className="agensi__fine">
              Free and security-scanned are Agensi listing attributes. Agent compatibility
              is as listed on Agensi. No Agensi logo is used here.
            </p>
          </div>

          <div className="agensi__session">
            <Terminal
              title="agensi / example session"
              lines={agensiLines}
              liveLabel="listed"
              caption="example session from the Agensi listing"
            />
            <div className="agensi__rows">
              <div className="agensi__row">
                <strong>goal-4</strong>
                ship checkout v2
                <Badge>goal / high</Badge>
              </div>
              <div className="agensi__row">
                <strong>task-12</strong>
                auth service is live (RS256)
                <Badge>done</Badge>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
