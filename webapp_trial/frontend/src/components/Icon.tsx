/** Font Awesome Sharp Light, the TR brand icon set (font bundled in /fonts). Inherits the text colour; never emoji. */
export type IconName =
  | "check" | "x" | "warn" | "clock" | "spin" | "minus" | "upload" | "list" | "folder" | "download"
  | "expand" | "eye" | "eye-off" | "send" | "scissors" | "flag" | "file" | "back";

export default function Icon({ name }: { name: IconName }) {
  return <i className={`fai fai-${name}`} aria-hidden="true" />;
}
