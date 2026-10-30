import { AGENSI_URL, INSTALL_CMD } from "../data/content";
import { useCopy } from "../hooks/useCopy";
import Button from "./ui/Button";
import Scramble from "./ui/Scramble";

const finalLine = "Stop paying the amnesia tax.";

export default function FinalCta() {
  const [, copy] = useCopy();

  return (
    <section className="final" aria-labelledby="final-title">
      <div className="grid-field" aria-hidden="true" />
      <div className="wrap final__inner">
        <span className="kicker">Get started</span>
        <h2 id="final-title" aria-label={finalLine}>
          <Scramble text={finalLine} />
        </h2>
        <p className="final__lede">
          One shared memory. Goals and tasks that survive session resets.
          A project changelog your agents actually write.
          Local, private, no account required.
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
          <span> mesh brief --max-tokens 500</span>
          <span className="terminal__cursor" aria-hidden="true" />
        </div>
      </div>
    </section>
  );
}
