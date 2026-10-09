interface Props {
  label: string;
  pct: number | null;
}

export default function ScoreCard({ label, pct }: Props) {
  const cls =
    pct === null ? "score-unknown" : pct >= 99 ? "score-good" : pct >= 95 ? "score-warn" : "score-bad";
  return (
    <div className={`score-card ${cls}`}>
      <span className="score-label">{label}</span>
      <span className="score-value">{pct === null ? "n/a" : `${pct.toFixed(2)}%`}</span>
    </div>
  );
}
