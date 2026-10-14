import { useState } from "react";
import { submitAllApproved } from "../api";
import Icon from "./Icon";

interface Props {
  keys?: string[];
  textPaths?: string[];
  /** Button text; defaults to "Submit all approved". */
  label?: string;
  disabled?: boolean;
  /** Called after files were sent, so the page can refresh its status. */
  onDone?: () => void;
}

/** "Submit all approved": sends only files that are approved (and unchanged since); everything else is skipped. */
export default function SubmitAllButton({ keys, textPaths, label, disabled, onDone }: Props) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  async function run() {
    setBusy(true);
    setMsg(null);
    setFailed(false);
    try {
      const plan = await submitAllApproved({ keys, textPaths }, true);
      if (plan.ready === 0) {
        setMsg(plan.skipped
          ? `Nothing to submit: ${plan.skipped} file(s) are not approved yet, or already submitted. Approve a file in its review screen first.`
          : "Nothing to submit.");
        return;
      }
      const names = plan.results.filter((r) => r.status === "ready").map((r) => `  • ${r.name}`);
      const left = plan.skipped ? `\n\n${plan.skipped} other file(s) are not approved or already sent and will be skipped.` : "";
      if (!window.confirm(`Submit ${plan.ready} approved file(s) to Keyed?\n\n${names.slice(0, 12).join("\n")}${names.length > 12 ? `\n  …and ${names.length - 12} more` : ""}${left}`)) return;
      const res = await submitAllApproved({ keys, textPaths }, false);
      setFailed(res.failed > 0);
      setMsg(
        `Submitted ${res.submitted}` + (res.skipped ? ` · skipped ${res.skipped}` : "") +
          (res.failed ? ` · FAILED ${res.failed}: ${res.results.filter((r) => r.status === "failed").map((r) => `${r.name} (${r.reason})`).join("; ")}` : "")
      );
      onDone?.();
    } catch (err) {
      setFailed(true);
      setMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="submit-all">
      <button onClick={run} disabled={busy || disabled} title="Sends only approved files. Anything not approved, or edited since, is skipped.">
        <Icon name="send" /> {busy ? "Submitting…" : label ?? "Submit all approved"}
      </button>
      {msg && <span className={failed ? "error submit-all-msg" : "muted submit-all-msg"}>{msg}</span>}
    </span>
  );
}
