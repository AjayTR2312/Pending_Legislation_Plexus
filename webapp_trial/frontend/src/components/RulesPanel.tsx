import { useEffect, useMemo, useState } from "react";
import { getRulesOverview } from "../api";
import type { RuleCatalogEntry, RulesOverview } from "../types";
import Icon from "./Icon";

const MARKUP: Record<string, string> = {
  underline: "underline", bold: "bold", italic: "italic", all_caps: "ALL CAPS", brackets: "brackets", strike_out: "strike-out",
};
const words = (keys: string[] | undefined) => (keys && keys.length ? keys.map((k) => MARKUP[k] ?? k).join(" + ") : "none");

function RuleList({ rules }: { rules: RuleCatalogEntry[] }) {
  return (
    <ul className="rule-list">
      {rules.map((r) => (
        <li key={r.id}>
          <b>{r.title}</b>
          <div className="muted">{r.requirement}</div>
        </li>
      ))}
    </ul>
  );
}

/** Every jurisdiction's rules, one collapsed section per state: what the spec says, what is checked, what is by eye. */
export default function RulesPanel() {
  const [data, setData] = useState<RulesOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [openAll, setOpenAll] = useState<boolean | null>(null);

  useEffect(() => {
    getRulesOverview().then(setData).catch((err) => setError(String(err)));
  }, []);

  const states = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (data?.states ?? []).filter((s) => !q || s.code.toLowerCase().includes(q) || (s.spec?.name ?? "").toLowerCase().includes(q));
  }, [data, query]);

  if (error) return <p className="error">{error}</p>;
  if (!data) return <div aria-busy="true"><div className="skeleton" style={{ width: "30%" }} /><div className="skeleton" /><div className="skeleton" /></div>;

  // changing openAll re-keys the sections so "Expand all / Collapse all" really applies to every one
  const key = (id: string) => `${id}-${openAll === null ? "x" : openAll ? "open" : "shut"}`;

  return (
    <div className="rules-panel">
      <h2 className="page-title">Rules by state</h2>
      <p className="muted">
        What each jurisdiction asks for, in its own words, and how this app checks it. <b>Checked automatically</b> rules run on
        every document of that state. <b>Check by eye</b> items cannot be read from text (underline versus bold, for example), so
        a person verifies them against the PDF.
      </p>

      <div className="rules-toolbar">
        <input type="search" placeholder="Find a state (name or code)" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Find a state" />
        <button className="secondary" onClick={() => setOpenAll(true)}>Expand all</button>
        <button className="secondary" onClick={() => setOpenAll(false)}>Collapse all</button>
        <span className="muted">{states.length} of {data.states.length} states</span>
      </div>

      <details className="card rules-state" key={key("global")} open={openAll === true ? true : undefined}>
        <summary>
          <Icon name="book" /> <strong>Rules for every state</strong>{" "}
          <span className="muted">{data.global.length} automatic checks</span>
        </summary>
        <RuleList rules={data.global} />
      </details>

      {states.map((s) => (
        <details className="card rules-state" key={key(s.code)} open={openAll === true ? true : undefined}>
          <summary>
            <span className="pill">{s.code}</span> <strong>{s.spec?.name ?? s.code}</strong>{" "}
            <span className="muted">
              adds: {words(s.spec?.add)} · deletes: {words(s.spec?.delete)} · {s.rules.length} own check{s.rules.length === 1 ? "" : "s"}
              {s.manual.length ? ` · ${s.manual.length} by eye` : ""}
            </span>
          </summary>

          <table className="rules-facts">
            <tbody>
              <tr><th>File type</th><td>{s.spec?.file_type || "—"}</td></tr>
              <tr><th>Additions</th><td>{words(s.spec?.add)}{s.spec?.all_caps_condition ? ` (ALL CAPS only ${s.spec.all_caps_condition})` : ""}</td></tr>
              <tr><th>Deletions</th><td>{words(s.spec?.delete)}</td></tr>
              {s.spec?.tag_scope && <tr><th>Tag scope</th><td>{s.spec.tag_scope.replace(/_/g, " ")}</td></tr>}
              <tr><th>Remove from output</th><td>{s.spec?.remove || "—"}</td></tr>
            </tbody>
          </table>

          {(s.instructions.add.length > 0 || s.instructions.delete.length > 0) && (
            <>
              <h3>Instructions from the spec</h3>
              <ul className="rule-list">
                {s.instructions.add.map((t, i) => <li key={`a${i}`}><span className="pill">add</span> {t}</li>)}
                {s.instructions.delete.map((t, i) => <li key={`d${i}`}><span className="pill">delete</span> {t}</li>)}
              </ul>
            </>
          )}

          {s.rules.length > 0 && (<><h3>Checked automatically (this state)</h3><RuleList rules={s.rules} /></>)}
          {s.manual.length > 0 && (
            <>
              <h3>Check by eye against the PDF</h3>
              <ul className="rule-list">
                {s.manual.map((m, i) => <li key={i}><span className="pill">{m.kind}</span> {m.text}</li>)}
              </ul>
            </>
          )}
        </details>
      ))}
    </div>
  );
}
