export default function SectionHeader({ index, title, lede, id, meta, route }) {
  return (
    <header className="section-head">
      <div className="section-head__index" aria-hidden="true">
        <span>{index}</span>
        <span className="section-head__rule" />
        {route && <span className="section-head__route">{route}</span>}
      </div>
      <div className="section-head__copy">
        <h2 id={id}>{title}</h2>
        {lede && <p className="section-head__lede">{lede}</p>}
      </div>
      {meta && <div className="section-head__meta">{meta}</div>}
    </header>
  );
}
