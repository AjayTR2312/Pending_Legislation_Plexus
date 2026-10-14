import { useMemo, useState } from "react";
import { diffWords, tokens, windowOf, type Tok } from "../wordDiff";

const LIMIT = 70;

function Block({ label, tone, toks, open }: { label: string; tone: "pdf" | "out"; toks: Tok[]; open: boolean }) {
  const { from, to } = open ? { from: 0, to: toks.length } : windowOf(toks, LIMIT);
  return (
    <div className={`fc-block fc-${tone}`}>
      <div className="fc-label">{label}</div>
      <p className="fc-text">
        {from > 0 && "… "}
        {toks.slice(from, to).map((x, i) => (
          <span key={from + i}>
            {x.c ? <mark className="fc-chg">{x.t}</mark> : x.t}{" "}
          </span>
        ))}
        {to < toks.length && "…"}
      </p>
    </div>
  );
}

/** "In the PDF" next to "In the converted text", with the words that differ highlighted. */
export default function FindingCompare({ expected, output, plain, notes = true }: { expected: string; output: string; plain?: boolean; notes?: boolean }) {
  const [open, setOpen] = useState(false);
  const d = useMemo(() => (!plain && expected && output ? diffWords(expected, output) : null), [expected, output, plain]);
  const plainToks = (s: string): Tok[] => tokens(s).map((t) => ({ t, c: false }));
  const left = d ? d.left : plainToks(expected);
  const right = d ? d.right : plainToks(output);
  const long = Math.max(left.length, right.length) > LIMIT + 10;
  return (
    <div className="fc">
      {expected && <Block label="In the PDF" tone="pdf" toks={left} open={open} />}
      {output && <Block label="In the converted text" tone="out" toks={right} open={open} />}
      {notes && !expected && output && <p className="fc-note">The PDF extraction has nothing here.</p>}
      {notes && expected && !output && <p className="fc-note">The converted text has nothing here.</p>}
      {long && (
        <button className="link-button fc-more" onClick={() => setOpen((v) => !v)}>
          {open ? "Show less" : `Show all (${Math.max(left.length, right.length)} words)`}
        </button>
      )}
    </div>
  );
}
