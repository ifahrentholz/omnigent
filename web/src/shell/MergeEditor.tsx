// Monaco merge editor for one conflicted file, modelled on VS Code's inline
// merge-conflict support: a read-only side-by-side diff of the task's and the
// base's versions on top, and the editable result below with the conflict
// regions colored and "Accept …" code lenses above each hunk. Lazy-loaded so
// Monaco stays out of the initial bundle.

import { DiffEditor, Editor, type DiffOnMount, type OnMount } from "@monaco-editor/react";
import { ChevronDownIcon, ChevronUpIcon } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import {
  codeFontFamilyForEditor,
  readCodeFont,
  subscribeCodeFont,
} from "@/lib/codeFontPreferences";
import { type HunkChoice, conflictRegions, hunkEdit } from "@/lib/conflictMarkers";
import { detectLang } from "./codeViewerHelpers";
import {
  type monaco,
  ensureLanguage,
  ensureMonacoReady,
  monacoLanguageId,
  resolvedThemeToMonaco,
} from "./monacoSetup";
import "./mergeEditor.css";

type CodeEditor = Parameters<OnMount>[0];
type MonacoApi = Parameters<OnMount>[1];

export interface MergeEditorProps {
  /** Workspace-relative path; picks the highlighting language. */
  path: string;
  /** The task branch's version of the file. */
  ours: string;
  /** The base's version of the file. */
  theirs: string;
  /** Header for the task side, e.g. "Task (omni/login-1)". */
  oursLabel: string;
  /** The base's name, e.g. "main". */
  base: string;
  /** The result, conflict markers included until resolved. */
  value: string;
  onChange: (text: string) => void;
}

const BUTTON =
  "inline-flex items-center rounded-full border border-border px-1.5 py-0.5 hover:bg-accent disabled:opacity-50";

function conflictDecorations(
  text: string,
  monacoApi: MonacoApi,
): monaco.editor.IModelDeltaDecoration[] {
  const lane = monacoApi.editor.OverviewRulerLane.Full;
  const block = (
    from: number,
    to: number,
    className: string,
    ruler?: string,
  ): monaco.editor.IModelDeltaDecoration[] =>
    to < from
      ? []
      : [
          {
            range: new monacoApi.Range(from, 1, to, 1),
            options: {
              isWholeLine: true,
              className,
              ...(ruler ? { overviewRuler: { color: ruler, position: lane } } : {}),
            },
          },
        ];
  return conflictRegions(text).flatMap((region) => [
    ...region.markers.flatMap((line, i) =>
      block(
        line,
        line,
        i === 0
          ? "merge-marker merge-marker-ours"
          : i === region.markers.length - 1
            ? "merge-marker merge-marker-theirs"
            : "merge-marker",
      ),
    ),
    ...block(region.ours.start, region.ours.end, "merge-ours", "#2ea04380"),
    ...(region.ancestor ? block(region.ancestor.start, region.ancestor.end, "merge-ancestor") : []),
    ...block(region.theirs.start, region.theirs.end, "merge-theirs", "#388bfd80"),
  ]);
}

/**
 * Two-pane merge view for one text file: task vs base on top, result below.
 *
 * @param props See {@link MergeEditorProps}.
 * @returns The merge editor, or a loading / error note while Monaco loads.
 */
export default function MergeEditor({
  path,
  ours,
  theirs,
  oursLabel,
  base,
  value,
  onChange,
}: MergeEditorProps) {
  const lang = detectLang(path);
  const theme = resolvedThemeToMonaco(useResolvedThemeMode());
  const [ready, setReady] = useState(false);
  const [loadError, setLoadError] = useState(false);
  useEffect(() => {
    let cancelled = false;
    setReady(false);
    setLoadError(false);
    void Promise.all([ensureMonacoReady(), ensureLanguage(lang)]).then(
      () => !cancelled && setReady(true),
      () => !cancelled && setLoadError(true),
    );
    return () => {
      cancelled = true;
    };
  }, [lang]);

  const editorRef = useRef<CodeEditor | null>(null);
  const monacoRef = useRef<MonacoApi | null>(null);
  const diffRef = useRef<Parameters<DiffOnMount>[0] | null>(null);
  const diffModelsRef = useRef<monaco.editor.ITextModel[]>([]);
  const disposablesRef = useRef<{ dispose: () => void }[]>([]);
  const decorationsRef = useRef<monaco.editor.IEditorDecorationsCollection | null>(null);
  const regions = useMemo(() => conflictRegions(value), [value]);

  const accept = useCallback((index: number, choice: HunkChoice) => {
    const editor = editorRef.current;
    const monacoApi = monacoRef.current;
    const model = editor?.getModel();
    if (!editor || !monacoApi || !model) return;
    const edit = hunkEdit(model.getValue(), index, choice);
    if (!edit) return;
    const range = new monacoApi.Range(
      edit.startLine,
      edit.startColumn,
      edit.endLine,
      edit.endColumn,
    );
    editor.pushUndoStop();
    editor.executeEdits("merge-conflict", [{ range, text: edit.text }]);
    editor.pushUndoStop();
    editor.focus();
  }, []);

  const reveal = useCallback(
    (direction: 1 | -1) => {
      const editor = editorRef.current;
      if (!editor || regions.length === 0) return;
      const line = editor.getPosition()?.lineNumber ?? 0;
      const starts = regions.map((r) => r.hunk.start);
      const target =
        direction === 1
          ? (starts.find((s) => s > line) ?? starts[0])
          : ([...starts].reverse().find((s) => s < line) ?? starts[starts.length - 1]);
      editor.setPosition({ lineNumber: target, column: 1 });
      editor.revealLineInCenter(target);
      editor.focus();
    },
    [regions],
  );

  const handleMount: OnMount = useCallback(
    (editor, monacoApi) => {
      editorRef.current = editor;
      monacoRef.current = monacoApi;
      decorationsRef.current = editor.createDecorationsCollection(
        conflictDecorations(editor.getValue(), monacoApi),
      );
      const commands = (["ours", "theirs", "both"] as const).map((choice) =>
        editor.addCommand(0, (_accessor: unknown, index: number) => accept(index, choice)),
      );
      const titles = [`Accept task`, `Accept ${base}`, "Accept both"];
      disposablesRef.current.push(
        monacoApi.languages.registerCodeLensProvider("*", {
          provideCodeLenses: (model: monaco.editor.ITextModel) => {
            if (model !== editor.getModel()) return { lenses: [], dispose: () => {} };
            const lenses = conflictRegions(model.getValue()).flatMap((region, index) =>
              commands.flatMap((id, i) =>
                id
                  ? [
                      {
                        range: new monacoApi.Range(region.hunk.start, 1, region.hunk.start, 1),
                        command: { id, title: titles[i], arguments: [index] },
                      },
                    ]
                  : [],
              ),
            );
            return { lenses, dispose: () => {} };
          },
        }),
        editor.onDidChangeModelContent(() => {
          decorationsRef.current?.set(conflictDecorations(editor.getValue(), monacoApi));
        }),
      );
      const first = conflictRegions(editor.getValue())[0];
      if (first) editor.revealLineInCenter(first.hunk.start);
    },
    [accept, base],
  );

  const handleDiffMount: DiffOnMount = useCallback((diffEditor) => {
    diffRef.current = diffEditor;
    const model = diffEditor.getModel();
    diffModelsRef.current = model ? [model.original, model.modified] : [];
  }, []);

  useEffect(
    () => () => {
      for (const d of disposablesRef.current) d.dispose();
      disposablesRef.current = [];
      // Detach the diff widget before its models go (see MonacoDiffViewer).
      try {
        diffRef.current?.setModel(null);
      } catch {
        // Already disposed by the library.
      }
      for (const model of diffModelsRef.current) {
        try {
          model.dispose();
        } catch {
          // Already disposed.
        }
      }
      diffModelsRef.current = [];
      editorRef.current = null;
      diffRef.current = null;
    },
    [],
  );

  useEffect(
    () =>
      subscribeCodeFont((font) => {
        const fontOptions = {
          fontSize: font.sizePx,
          fontFamily: codeFontFamilyForEditor(font.family),
          fontWeight: String(font.weight),
        };
        editorRef.current?.updateOptions(fontOptions);
        diffRef.current?.updateOptions(fontOptions);
      }),
    [],
  );

  const fontOptions = useMemo(() => {
    const font = readCodeFont();
    return {
      fontSize: font.sizePx,
      fontFamily: codeFontFamilyForEditor(font.family),
      fontWeight: String(font.weight),
      minimap: { enabled: false },
      automaticLayout: true,
      scrollBeyondLastLine: false,
    };
  }, []);

  if (loadError) return <p className="text-sm text-destructive">Failed to load the editor.</p>;
  if (!ready) return <p className="text-sm text-muted-foreground">Loading editor…</p>;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2 text-xs">
      <div className="flex min-h-0 flex-[2] flex-col overflow-hidden rounded border border-border">
        <div className="grid grid-cols-2 divide-x divide-border border-b border-border text-muted-foreground">
          <span className="truncate px-2 py-1">{oursLabel}</span>
          <span className="truncate px-2 py-1">Base ({base})</span>
        </div>
        <div className="min-h-0 flex-1" data-testid="merge-sides">
          <DiffEditor
            height="100%"
            theme={theme}
            language={monacoLanguageId(lang)}
            original={ours}
            modified={theirs}
            onMount={handleDiffMount}
            keepCurrentOriginalModel
            keepCurrentModifiedModel
            options={{
              ...fontOptions,
              readOnly: true,
              originalEditable: false,
              renderSideBySide: true,
              useInlineViewWhenSpaceIsLimited: false,
              renderOverviewRuler: false,
              hideUnchangedRegions: { enabled: true, contextLineCount: 3 },
            }}
          />
        </div>
      </div>
      <div className="flex min-h-0 flex-[3] flex-col overflow-hidden rounded border border-border">
        <div className="flex items-center gap-2 border-b border-border px-2 py-1">
          <span className="font-medium">
            Result <span className="font-mono">{path}</span>
          </span>
          <span className="text-muted-foreground">
            {regions.length === 0 ? "· no conflicts left" : `· ${regions.length} conflict(s) left`}
          </span>
          <span className="ml-auto flex gap-1">
            <button
              type="button"
              aria-label="Previous conflict"
              title="Previous conflict"
              disabled={regions.length === 0}
              onClick={() => reveal(-1)}
              className={BUTTON}
            >
              <ChevronUpIcon aria-hidden="true" className="size-3" />
            </button>
            <button
              type="button"
              aria-label="Next conflict"
              title="Next conflict"
              disabled={regions.length === 0}
              onClick={() => reveal(1)}
              className={BUTTON}
            >
              <ChevronDownIcon aria-hidden="true" className="size-3" />
            </button>
          </span>
        </div>
        <div className="min-h-0 flex-1" data-testid="merge-result">
          <Editor
            height="100%"
            theme={theme}
            language={monacoLanguageId(lang)}
            value={value}
            onChange={(text) => onChange(text ?? "")}
            onMount={handleMount}
            options={{ ...fontOptions, codeLens: true, glyphMargin: false }}
          />
        </div>
      </div>
    </div>
  );
}
