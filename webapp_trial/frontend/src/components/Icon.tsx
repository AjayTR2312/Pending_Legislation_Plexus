/** Font Awesome Sharp Light, the TR brand icon set (font bundled in /fonts). Inherits the text colour; never emoji. */
export type IconName =
  | "check" | "x" | "warn" | "clock" | "spin" | "minus" | "upload" | "list" | "folder" | "download"
  | "book" | "info" | "compress" | "chevron-down" | "chevron-right" | "chevron-left" | "expand" | "eye" | "eye-off" | "send" | "scissors" | "flag" | "file" | "back";

export default function Icon({ name }: { name: IconName }) {
  return <i className={`fai fai-${name}`} aria-hidden="true" />;
}
