import { AGENSI_URL } from "../data/content";
import Badge from "./ui/Badge";
import Button from "./ui/Button";
import SectionHeader from "./ui/SectionHeader";
import Terminal from "./ui/Terminal";

const agensiLines = [
  '$ memory: "staging db moved to :5439"',
  "ok  updated db-cfg-92",
  "ok  resolved conf-412",
  "ok  all agents recall :5439",
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
          lede="Mesh is published as a free, security-scanned SKILL.md skill on Agensi. Use it from Claude Code, Cursor, Codex CLI, Muse, Dots, OpenClaw, Manus, Grok, and 18 more — or install the package yourself."
          meta="listing attributes / via Agensi"
        />

        <div className="agensi__panel">
          <div className="agensi__copy">
            <h3>The skill is free. The memory is yours.</h3>
            <div className="agensi__badges">
              <Badge tone="accent">Free</Badge>
              <Badge tone="ok">Security scanned</Badge>
              <Badge>27 agents</Badge>
            </div>
            <p>
              The listing includes a worked session, the install flow, and the agents it
              supports. One click gets the skill; the package is the same local engine.
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
                <strong>db-cfg-92</strong>
                staging database port is 5439
                <Badge>fact</Badge>
              </div>
              <div className="agensi__row">
                <strong>conf-412</strong>
                overwrote old port (5432)
                <Badge>conflict</Badge>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
