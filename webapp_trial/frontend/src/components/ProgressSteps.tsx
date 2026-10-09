import Icon, { type IconName } from "./Icon";
import type { StepLogEntry } from "../types";

const STEP_LABELS: Record<string, string> = {
  upload: "Upload received",
  detect_state: "Detect state",
  convert: "Hybrid conversion (PyMuPDF + Claude)",
  postprocess: "Deterministic post-processing",
  save: "Save output",
};

const ICONS: Record<string, IconName> = {
  done: "check",
  running: "spin",
  error: "x",
  warning: "warn",
  info: "spin",
};

export default function ProgressSteps({ steps }: { steps: StepLogEntry[] }) {
  // Keep only the latest entry per step (a step can log "running" then "done").
  const latestByStep = new Map<string, StepLogEntry>();
  for (const entry of steps) latestByStep.set(entry.step, entry);

  return (
    <ul className="progress-steps">
      {Object.keys(STEP_LABELS).map((step) => {
        const entry = latestByStep.get(step);
        const icon: IconName = entry ? ICONS[entry.status] : "clock";
        return (
          <li key={step} className={entry ? `step-${entry.status}` : "step-pending"}>
            <span className="step-icon"><Icon name={icon} /></span>
            <span className="step-label">{STEP_LABELS[step]}</span>
            {entry?.message && <span className="step-message">{entry.message}</span>}
          </li>
        );
      })}
    </ul>
  );
}
