"use client";

/**
 * Mix design upload, parse, and confirmation (prompt §7.0).
 *
 * Shared by `/projects/new` (initial onboarding) and `/projects/[id]` (a new
 * submittal revision). The flow is upload → AI parse → confirm, and nothing
 * is written until the parent screen saves: the parse endpoint stores the PDF
 * and returns rows, but writes no mix_designs rows. That is deliberate — §7.0
 * requires the parsed table be shown for confirmation first, because a
 * plausible wrong row is harder to catch later than a blank one.
 */

import { useState } from "react";
import { api, formatApiError } from "@/lib/api";
import {
  confidenceLabel,
  confidencePillClass,
  type CostCode,
  type MixDesignParseResult,
  type ParsedMixRow,
} from "@/lib/types";

export interface MixDesignState {
  revision: string;
  storagePath: string | null;
  filename: string | null;
  rows: ParsedMixRow[];
  notes: string[];
  warnings: string[];
  model: string | null;
}

export const emptyMixDesign: MixDesignState = {
  revision: "CMD-01",
  storagePath: null,
  filename: null,
  rows: [],
  notes: [],
  warnings: [],
  model: null,
};

export function MixDesignEditor({
  value,
  onChange,
  costCodes,
  projectId,
  projectNo,
  projectName,
  disabled = false,
}: {
  value: MixDesignState;
  onChange: (next: MixDesignState) => void;
  costCodes: CostCode[];
  projectId?: string;
  projectNo?: string;
  projectName?: string;
  disabled?: boolean;
}) {
  const [parsing, setParsing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleFile(file: File) {
    setError(null);
    setParsing(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("revision", value.revision || "CMD-01");
      if (projectId) fd.append("project_id", projectId);
      if (projectNo) fd.append("project_no", projectNo);
      if (projectName) fd.append("project_name", projectName);

      const result = await api.post<MixDesignParseResult>(
        "/projects/parse-mix-design",
        undefined,
        { formData: fd }
      );

      onChange({
        // Trust the revision printed on the document over the typed default,
        // but keep the typed one if the document does not name one.
        revision: result.revision || value.revision || "CMD-01",
        storagePath: result.storage_path,
        filename: result.original_filename,
        rows: result.rows,
        notes: result.notes,
        warnings: result.warnings,
        model: result.model,
      });
    } catch (e) {
      setError(formatApiError(e));
    } finally {
      setParsing(false);
    }
  }

  function updateRow(index: number, patch: Partial<ParsedMixRow>) {
    const rows = value.rows.map((r, i) => (i === index ? { ...r, ...patch } : r));
    onChange({ ...value, rows });
  }

  function removeRow(index: number) {
    onChange({ ...value, rows: value.rows.filter((_, i) => i !== index) });
  }

  function addRow() {
    onChange({
      ...value,
      rows: [
        ...value.rows,
        {
          mix_no: "",
          psi: null,
          element_use: [],
          pump_line: null,
          cost_code_id: null,
          proposed_code: null,
          confidence: null,
          rationale: null,
        },
      ],
    });
  }

  const unmapped = value.rows.filter((r) => !r.cost_code_id).length;

  return (
    <div>
      {error && (
        <div className="callout callout-block">
          <div className="callout-title">Could not parse the submittal</div>
          {error}
        </div>
      )}

      <div className="form-grid-3" style={{ marginBottom: 16 }}>
        <div className="form-row" style={{ marginBottom: 0 }}>
          <label className="form-label" htmlFor="mix-revision">
            Revision
          </label>
          <input
            id="mix-revision"
            className="input"
            value={value.revision}
            disabled={disabled}
            onChange={(e) => onChange({ ...value, revision: e.target.value })}
            placeholder="CMD-01"
          />
          <div className="form-help">
            The submittal number. A new revision supersedes the old rows
            without deleting them.
          </div>
        </div>

        <div
          className="form-row"
          style={{ marginBottom: 0, gridColumn: "span 2" }}
        >
          <label className="form-label" htmlFor="mix-file">
            Mix design submittal (PDF)
          </label>
          <div className="dropzone">
            <div className="dropzone-title">
              {parsing
                ? "Reading the yardage sheet…"
                : "Choose the supplier's submittal PDF"}
            </div>
            <div className="dropzone-hint">
              Claude reads the yardage sheet and proposes a cost code per mix.
              You confirm every row before anything is saved.
            </div>
            <input
              id="mix-file"
              type="file"
              accept="application/pdf,.pdf"
              disabled={disabled || parsing}
              style={{ marginTop: 12 }}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) handleFile(file);
              }}
            />
            {value.filename && (
              <div className="dropzone-file">{value.filename}</div>
            )}
          </div>
        </div>
      </div>

      {value.notes.length > 0 && (
        <div className="callout callout-warn">
          <div className="callout-title">Check these by eye</div>
          <ul>
            {value.notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        </div>
      )}

      {value.warnings.length > 0 && (
        <div className="callout callout-block">
          <div className="callout-title">Rows that need your attention</div>
          <ul>
            {value.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </div>
      )}

      {value.rows.length > 0 && (
        <>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Mix no.</th>
                  <th className="num">PSI</th>
                  <th>Element uses</th>
                  <th>Pump line</th>
                  <th>Cost code</th>
                  <th>AI</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {value.rows.map((row, i) => (
                  <tr key={i}>
                    <td className="mono">
                      <input
                        className="cell-select"
                        style={{ minWidth: 110 }}
                        value={row.mix_no}
                        disabled={disabled}
                        onChange={(e) =>
                          updateRow(i, { mix_no: e.target.value })
                        }
                        placeholder="4018045"
                      />
                    </td>
                    <td className="num">
                      <input
                        className="cell-select"
                        style={{ minWidth: 72, textAlign: "right" }}
                        value={row.psi ?? ""}
                        disabled={disabled}
                        inputMode="numeric"
                        onChange={(e) => {
                          const digits = e.target.value.replace(/[^0-9]/g, "");
                          updateRow(i, {
                            psi: digits === "" ? null : parseInt(digits, 10),
                          });
                        }}
                        placeholder="4000"
                      />
                    </td>
                    <td>
                      <input
                        className="cell-select"
                        value={row.element_use.join(", ")}
                        disabled={disabled}
                        onChange={(e) =>
                          updateRow(i, {
                            element_use: e.target.value
                              .split(",")
                              .map((s) => s.trim())
                              .filter(Boolean),
                          })
                        }
                        placeholder="Columns, Basement Walls"
                      />
                      <span className="cell-sub">
                        Comma separated. One mix commonly serves several.
                      </span>
                    </td>
                    <td className="mono">
                      <input
                        className="cell-select"
                        style={{ minWidth: 92 }}
                        value={row.pump_line ?? ""}
                        disabled={disabled}
                        onChange={(e) =>
                          updateRow(i, { pump_line: e.target.value || null })
                        }
                        placeholder='3" LINE'
                      />
                    </td>
                    <td>
                      <select
                        className="cell-select"
                        value={row.cost_code_id ?? ""}
                        disabled={disabled}
                        onChange={(e) =>
                          updateRow(i, { cost_code_id: e.target.value || null })
                        }
                      >
                        <option value="">Not mapped</option>
                        {costCodes.map((c) => (
                          <option key={c.id} value={c.id}>
                            {c.code}
                          </option>
                        ))}
                      </select>
                      {!row.cost_code_id && (
                        <span className="cell-sub">
                          Unmapped mixes are never used to suggest a concrete
                          code.
                        </span>
                      )}
                    </td>
                    <td>
                      <span
                        className={`pill ${confidencePillClass(row.confidence)}`}
                      >
                        {confidenceLabel(row.confidence)}
                      </span>
                      {row.rationale && (
                        <span className="cell-sub">{row.rationale}</span>
                      )}
                    </td>
                    <td>
                      {!disabled && (
                        <button
                          type="button"
                          className="btn btn-ghost"
                          style={{ padding: "4px 10px", fontSize: 12 }}
                          onClick={() => removeRow(i)}
                        >
                          Remove
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 12,
              marginTop: 14,
              flexWrap: "wrap",
            }}
          >
            {!disabled && (
              <button type="button" className="btn" onClick={addRow}>
                Add a mix row
              </button>
            )}
            <div style={{ fontSize: 12.5, color: "var(--text-faint)" }}>
              {value.rows.length} mix{value.rows.length === 1 ? "" : "es"}
              {unmapped > 0 && ` · ${unmapped} not mapped to a cost code`}
              {value.model && ` · read by ${value.model}`}
            </div>
          </div>
        </>
      )}

      {value.rows.length === 0 && !parsing && (
        <div className="callout callout-info">
          <div className="callout-title">No mix rows yet</div>
          Upload the submittal to have the yardage sheet read for you, or add
          rows by hand if the PDF will not parse.
          {!disabled && (
            <div style={{ marginTop: 10 }}>
              <button type="button" className="btn" onClick={addRow}>
                Add a mix row by hand
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
