import { useEffect, useMemo, useRef, useState } from "react";
import { comparePageUrl, getCompare } from "../api";
import type { CompareData } from "../types";

interface Props {
  textPath: string;
  pdfPath: string;
  editKey?: string;
}

const TAG_RE = /\[A>|<A\]|\[D>|<D\]/g;

/** Side-by-side PDF and text with word-level linking in both directions. */
export default function CompareViewer({ textPath, pdfPath, editKey }: Props) {
  const [data, setData] = useState<CompareData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const pageRefs = useRef<Record<number, HTMLDivElement | null>>({});
  const wordRefs = useRef<Record<string, HTMLSpanElement | null>>({});

  useEffect(() => {
    setData(null);
    setError(null);
    getCompare(textPath, pdfPath, editKey)
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, [textPath, pdfPath, editKey]);

  const active = activeKey && data ? data.map[activeKey] : null;

  // Reverse lookup so clicking the PDF can find the matching text word.
  const byPage = useMemo(() => {
    if (!data) return {};
    const out: Record<number, { key: string; box: CompareData["map"][string] }[]> = {};
    for (const [key, box] of Object.entries(data.map)) {
      (out[box.page] ??= []).push({ key, box });
    }
    return out;
  }, [data]);

  function selectKey(key: string, scrollPdf: boolean) {
    setActiveKey(key);
    const box = data?.map[key];
    if (scrollPdf && box) {
      pageRefs.current[box.page]?.scrollIntoView({ behavior: "smooth", block: "center" });
    } else if (!scrollPdf) {
      wordRefs.current[key]?.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  if (error) return <p className="error">{error}</p>;
  if (!data) return <p className="muted">Building word-level alignment…</p>;

  const coverage = data.pdf_words
    ? Math.round((data.mapped_words / data.pdf_words) * 100)
    : 0;

  return (
    <>
      <p className="muted compare-stats">
        {data.pages.length} pages · {data.mapped_words.toLocaleString()} of{" "}
        {data.pdf_words.toLocaleString()} PDF words linked ({coverage}%) · click any word to
        locate it on the other side
      </p>

      <div className="compare-grid">
        <div className="compare-pane">
          <h3>Source PDF</h3>
          <div className="pdf-scroll">
            {data.pages.map((p) => (
              <div
                key={p.index}
                className="pdf-page"
                ref={(el) => {
                  pageRefs.current[p.index] = el;
                }}
                style={{ aspectRatio: `1 / ${p.aspect}` }}
              >
                <img src={comparePageUrl(data.key, p.index)} alt={`Page ${p.index + 1}`} />

                {/* Clickable boxes over every mapped word on this page. */}
                {(byPage[p.index] ?? []).map(({ key, box }) => (
                  <span
                    key={key}
                    className={key === activeKey ? "wbox active" : "wbox"}
                    style={{
                      left: `${box.x * 100}%`,
                      top: `${box.y * 100}%`,
                      width: `${box.w * 100}%`,
                      height: `${box.h * 100}%`,
                    }}
                    onClick={() => selectKey(key, false)}
                  />
                ))}

                <span className="page-label">Page {p.index + 1}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="compare-pane">
          <h3>Converted text</h3>
          <div className="text-scroll">
            {data.lines.map((line, li) => (
              <div key={li} className="text-line">
                <span className="line-no">{li + 1}</span>
                <span className="line-body">
                  {line.split(" ").map((word, wi) => {
                    const key = `${li}_${wi}`;
                    const linked = key in data.map;
                    const bare = word.replace(TAG_RE, "");
                    const isAdd = word.includes("[A>") || word.includes("<A]");
                    const isDel = word.includes("[D>") || word.includes("<D]");
                    return (
                      <span
                        key={wi}
                        ref={(el) => {
                          wordRefs.current[key] = el;
                        }}
                        className={[
                          "tw",
                          linked ? "linked" : "unlinked",
                          key === activeKey ? "active" : "",
                          isAdd ? "tag-add" : "",
                          isDel ? "tag-del" : "",
                        ]
                          .filter(Boolean)
                          .join(" ")}
                        onClick={() => linked && selectKey(key, true)}
                      >
                        {bare}{" "}
                      </span>
                    );
                  })}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {active && (
        <p className="muted">
          Selected word is on page {active.page + 1}.
        </p>
      )}
    </>
  );
}
