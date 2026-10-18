import { AGENSI_URL, GITHUB_URL, INSTALL_CMD } from "../data/content";
import { useCopy } from "../hooks/useCopy";
import Button from "./ui/Button";
import CopyButton from "./ui/CopyButton";
import Icon from "./ui/Icon";
import SectionHeader from "./ui/SectionHeader";

const addCmd = 'mesh add "PostgreSQL 16 runs on port 5432" --tags db,prod --type fact';
const askCmd = 'mesh ask "what port does postgres use?" --n 3';

const created = [
  ["~/.mesh/", "mode 0700"],
  ["~/.mesh/mesh_<namespace>.db", "SQLite WAL"],
  ["~/.mesh/models/", "optional ONNX weights"],
  ["~/.mesh/auth_token", "mode 0600"],
];

export default function InstallSection() {
  const [, copy] = useCopy();

  return (
    <section className="section" id="install" aria-labelledby="install-title">
      <div className="wrap">
        <SectionHeader
          index="08"
          route="GET STARTED"
          id="install-title"
          title="Three commands to a shared memory."
          lede="Install the package, run setup, teach it one thing. Mesh does the rest locally."
          meta="no API key / no GPU"
        />

        <div className="install__grid">
          <div className="install-shell">
            <div className="install-shell__bar">terminal / install</div>
            <div className="install-shell__body">
              <div className="install-step">
                <span className="install-step__num">01</span>
                <div>
                  <div className="install-step__label">Install the package</div>
                  <div className="install-step__cmd"><span>$ </span>{INSTALL_CMD}</div>
                </div>
                <CopyButton text={INSTALL_CMD} compact label="Copy install command" />
              </div>

              <div className="install-step">
                <span className="install-step__num">02</span>
                <div>
                  <div className="install-step__label">Run setup</div>
                  <div className="install-step__cmd"><span>$ </span>mesh setup</div>
                  <p className="install-step__note">
                    Asks y/n before starting the daemon, registering auto-start, or
                    downloading the ~90 MB model.
                  </p>
                </div>
                <CopyButton text="mesh setup" compact label="Copy setup command" />
              </div>

              <div className="install-step">
                <span className="install-step__num">03</span>
                <div>
                  <div className="install-step__label">Teach it something</div>
                  <div className="install-step__cmd"><span>$ </span>{addCmd}</div>
                  <div className="install-step__cmd"><span>$ </span>{askCmd}</div>
                </div>
                <CopyButton
                  text={`${addCmd}\n${askCmd}`}
                  compact
                  label="Copy commands"
                />
              </div>
            </div>
          </div>

          <div className="created">
            <div className="created__head">what gets created</div>
            <div className="created__list">
              {created.map(([path, note]) => (
                <div className="created__item" key={path}>
                  <Icon name="check" size={13} />
                  <span>
                    <code>{path}</code> — {note}
                  </span>
                </div>
              ))}
            </div>
            <p className="created__empty">No silent background daemons or downloads.</p>
          </div>
        </div>

        <div className="install__actions">
          <Button icon="copy" onClick={() => copy(INSTALL_CMD, "Install command copied")}>
            Copy install command
          </Button>
          <Button href={GITHUB_URL} variant="secondary" iconRight="arrowUpRight">
            View on GitHub
          </Button>
          <Button href={AGENSI_URL} variant="quiet" iconRight="arrowUpRight">
            Use on Agensi
          </Button>
        </div>
        <p className="install__meta">Python 3.10+ · MIT · v0.7.1 · local-first</p>
      </div>
    </section>
  );
}
