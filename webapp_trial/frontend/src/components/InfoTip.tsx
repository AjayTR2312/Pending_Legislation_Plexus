import Icon from "./Icon";

/** A small (i): the longer explanation appears on hover or keyboard focus, so it does not take space on the card. */
export default function InfoTip({ text }: { text: string }) {
  return (
    <span className="info-tip" tabIndex={0} role="note" aria-label={text}>
      <Icon name="info" />
      <span className="tip-body" role="tooltip">{text}</span>
    </span>
  );
}
