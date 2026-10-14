import type { ReactNode } from "react";
import InfoTip from "./InfoTip";

interface Props {
  label: string;
  pct: number | null;
  /** One line of context under the number. */
  sub?: string;
  /** A button shown in the card (e.g. Run tag audit). */
  action?: ReactNode;
  /** Longer explanation, shown on hover of the (i). */
  info?: string;
}

export default function ScoreCard({ label, pct, sub, action, info }: Props) {
  const cls =
    pct === null ? "score-unknown" : pct >= 99 ? "score-good" : pct >= 95 ? "score-warn" : "score-bad";
  return (
    <div className={`score-card ${cls}`}>
      <span className="score-label">{label}{info && <InfoTip text={info} />}</span>
      <span className="score-value">{pct === null ? "n/a" : `${pct.toFixed(2)}%`}</span>
      {sub && <span className="score-sub">{sub}</span>}
      {action}
    </div>
  );
}
