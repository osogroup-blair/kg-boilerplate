import { useEffect, useState } from "react";
import { Banner, btn, inputCls } from "../../components/ui";
import { useVersionCtx } from "./VersionLayout";

/** Power-user view of the same draft. Saving goes through the same PUT as the forms, so the same validation runs. */
export function YamlTab() {
  const { version, readOnly, dirty, saveContent, saving } = useVersionCtx();
  const [text, setText] = useState(version.source_text ?? "");
  useEffect(() => setText(version.source_text ?? ""), [version.id, version.revision, version.source_text]);
  const changed = text !== (version.source_text ?? "");
  return (
    <div className="space-y-3">
      {dirty && !readOnly ? <Banner tone="amber">The forms have unsaved changes. Saving the YAML replaces them with this text.</Banner> : null}
      <textarea aria-label="Ontology YAML" rows={28} spellCheck={false} readOnly={readOnly} value={text} onChange={(e) => setText(e.target.value)} className={`${inputCls} w-full font-mono text-xs leading-relaxed`} />
      {!readOnly ? (
        <div className="flex gap-2">
          <button type="button" className={btn.primary} disabled={!changed || saving} onClick={() => void saveContent(text, "yaml")}>Save YAML</button>
          <button type="button" className={btn.secondary} disabled={!changed} onClick={() => setText(version.source_text ?? "")}>Discard</button>
        </div>
      ) : null}
    </div>
  );
}
