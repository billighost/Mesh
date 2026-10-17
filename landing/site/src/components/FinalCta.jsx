import { AGENSI_URL, INSTALL_CMD } from "../data/content";
import { useCopy } from "../hooks/useCopy";
import Button from "./ui/Button";
import Scramble from "./ui/Scramble";

const finalLine = "One agent learns. Every agent knows.";

export default function FinalCta() {
  const [, copy] = useCopy();

  return (
    <section className="final" aria-labelledby="final-title">
      <div className="grid-field" aria-hidden="true" />
      <div className="wrap final__inner">
        <span className="kicker">Final</span>
        <h2 id="final-title" aria-label={finalLine}>
          <Scramble text={finalLine} />
        </h2>
        <p className="final__lede">
          Install Mesh, teach it once, and stop paying the amnesia tax.
        </p>
        <div className="final__actions">
          <Button icon="copy" onClick={() => copy(INSTALL_CMD, "Install command copied")}>
            Install mesh
          </Button>
          <Button href={AGENSI_URL} variant="secondary" iconRight="arrowUpRight">
            Use on Agensi
          </Button>
        </div>
        <div className="final__cmd">
          <span className="term-dim">$</span>
          <span> mesh ask "what did we already learn?"</span>
          <span className="terminal__cursor" aria-hidden="true" />
        </div>
      </div>
    </section>
  );
}
