import type { ReactNode } from "react";

/** The package is DATA. It is rendered as text nodes only: React escapes everything, so a memory that says `<img onerror=...>` is shown as those characters. The only markup added
 * is a link around each citation handle the ENGINE wrote inside a trailing `[F:… · E:…]` group (the neutraliser guarantees nothing inside an item can form one). */
const GROUP = /\[([^\]\n]{1,300})\]/g;
const HANDLE = /^[A-Z]:[A-Za-z0-9_.-]{1,64}$/;

export function HandleText({ text, onPick }: { text: string; onPick?: (handle: string) => void }) {
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(GROUP)) {
    const start = m.index ?? 0;
    if (start > last) out.push(text.slice(last, start));
    const parts = m[1]!.split(" · ");
    out.push("[");
    parts.forEach((p, i) => {
      if (i > 0) out.push(" · ");
      out.push(HANDLE.test(p) ? <a key={`${start}:${i}`} href={`#cite-${p}`} data-handle={p} className="underline" onClick={() => onPick?.(p)}>{p}</a> : p);
    });
    out.push("]");
    last = start + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return <>{out}</>;
}
