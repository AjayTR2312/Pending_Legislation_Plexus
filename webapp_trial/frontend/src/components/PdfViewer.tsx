export default function PdfViewer({ src }: { src: string }) {
  // Relies on the browser's built-in PDF renderer — sufficient for the trial;
  // a pdf.js-based viewer can replace this later for page-level comparison.
  return (
    <object data={src} type="application/pdf" className="pdf-viewer">
      <p>
        PDF preview unavailable in this browser. <a href={src}>Open the PDF directly</a>.
      </p>
    </object>
  );
}
