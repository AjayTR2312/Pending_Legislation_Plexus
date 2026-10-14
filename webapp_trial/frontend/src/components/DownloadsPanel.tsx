import { useCallback, useEffect, useState } from "react";
import { checkSftp, getDownloads, queueZips } from "../api";
import type { DownloadsInfo } from "../types";
import Icon from "./Icon";

interface Props {
  onBatchCreated: (batchId: string) => void;
}

/** Files fetched from the SFTP server: check for new ones, pick ZIPs, and queue their PDFs for conversion. */
export default function DownloadsPanel({ onBatchCreated }: Props) {
  const [info, setInfo] = useState<DownloadsInfo | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<"check" | "queue" | null>(null);
  const [msg, setMsg] = useState<{ text: string; bad: boolean } | null>(null);

  const load = useCallback(async () => {
    try {
      setInfo(await getDownloads());
    } catch (err) {
      setMsg({ text: err instanceof Error ? err.message : String(err), bad: true });
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function check() {
    setBusy("check");
    setMsg(null);
    try {
      const r = await checkSftp();
      setMsg({ text: r.downloaded.length ? `Downloaded ${r.downloaded.length} new file(s): ${r.downloaded.join(", ")}` : "No new files on the SFTP server.", bad: false });
      await load();
    } catch (err) {
      setMsg({ text: err instanceof Error ? err.message : String(err), bad: true });
    } finally {
      setBusy(null);
    }
  }

  async function queue() {
    const names = [...picked];
    if (!window.confirm(
      `Extract and convert ${names.length} ZIP file(s)?\n\nEvery PDF inside becomes a job that the workers convert with the model, ` +
      `which COSTS TOKENS. Nothing is sent to Keyed: you review and approve each file first.`
    )) return;
    setBusy("queue");
    setMsg(null);
    try {
      const r = await queueZips(names);
      if (r.jobs.length === 0) {
        setMsg({ text: `Nothing was queued. ${r.skipped.map((s) => `${s.zip}: ${s.reason}`).join(" · ")}`, bad: true });
        await load();
      } else {
        onBatchCreated(r.batch_id);
      }
    } catch (err) {
      setMsg({ text: err instanceof Error ? err.message : String(err), bad: true });
    } finally {
      setBusy(null);
    }
  }

  const toggle = (name: string) =>
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  const files = info?.files ?? [];
  const selectable = files.filter((f) => !f.queued && f.state);

  return (
    <div className="card downloads">
      <h2 className="page-title">Downloaded files</h2>
      <p className="muted">
        ZIPs fetched from the SFTP server. Choose some and queue them: each PDF becomes a job, is converted, and appears
        in <b>Jobs</b> for review. Submit to Keyed happens after you approve.
      </p>
      {info && <p className="muted"><Icon name="folder" /> <code>{info.folder}</code></p>}

      <div className="downloads-actions">
        <button onClick={check} disabled={busy !== null || (info !== null && !info.sftp_configured)}
                title={info && !info.sftp_configured ? "FTP_PASSWORD is not set in the project .env" : "Connects to the SFTP server and downloads ZIPs you do not have yet"}>
          <Icon name="download" /> {busy === "check" ? "Connecting to SFTP…" : "Check SFTP for new files"}
        </button>
        {info && !info.sftp_configured && <span className="error">FTP_PASSWORD is not set in the project .env.</span>}
      </div>

      {msg && <p className={msg.bad ? "error" : "notice"} role={msg.bad ? "alert" : "status"}>{msg.text}</p>}

      {info === null ? (
        <div aria-busy="true"><div className="skeleton" /><div className="skeleton" style={{ width: "70%" }} /></div>
      ) : files.length === 0 ? (
        <div className="empty-state compact"><Icon name="download" />No ZIP files here yet. Click “Check SFTP for new files”.</div>
      ) : (
        <>
          <table className="zip-table">
            <thead>
              <tr>
                <th><input type="checkbox" aria-label="Select all"
                           checked={selectable.length > 0 && selectable.every((f) => picked.has(f.name))}
                           onChange={(e) => setPicked(e.target.checked ? new Set(selectable.map((f) => f.name)) : new Set())} /></th>
                <th>ZIP file</th><th>State</th><th>Size</th><th>Downloaded</th><th>Status</th>
              </tr>
            </thead>
            <tbody>
              {files.map((f) => {
                const can = !f.queued && !!f.state;
                return (
                  <tr key={f.name} className={can ? "" : "zip-off"}>
                    <td><input type="checkbox" disabled={!can} checked={picked.has(f.name)} onChange={() => toggle(f.name)} aria-label={`Select ${f.name}`} /></td>
                    <td className="zip-name" title={f.name}>{f.name}</td>
                    <td>{f.state ? <span className="pill">{f.state}</span> : "—"}</td>
                    <td>{f.size_kb.toLocaleString()} KB</td>
                    <td>{f.modified}</td>
                    <td>{f.queued ? "Already queued" : f.state ? "Ready" : "State not in the file name"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="downloads-actions">
            <button onClick={queue} disabled={busy !== null || picked.size === 0}>
              <Icon name="list" /> {busy === "queue" ? "Queuing…" : `Extract & convert${picked.size ? ` (${picked.size})` : ""}`}
            </button>
            <span className="tile-warn" title="Converting uses the model, so it costs tokens. You will be asked to confirm."><Icon name="warn" /> Costs tokens</span>
          </div>
        </>
      )}
    </div>
  );
}
