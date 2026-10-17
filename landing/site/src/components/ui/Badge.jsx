export default function Badge({ children, tone = "default", className = "" }) {
  return <span className={`badge badge--${tone} ${className}`}>{children}</span>;
}
