export function money(v: string | number | null | undefined, currency?: string): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  const s = Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${n < 0 ? "−" : ""}${currency ? currency + " " : ""}${s}`;
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export const CATEGORY_LABEL: Record<string, string> = {
  calculation_error: "Calculation error",
  contract_interpretation: "Contract interpretation",
  data_quality: "Data quality",
  customer_misunderstanding: "Not supported by evidence",
  needs_evidence: "Needs evidence",
};

export const STATUS_LABEL: Record<string, string> = {
  draft: "Not analysed", in_review: "In review", awaiting_info: "Awaiting information", resolved: "Resolved", reopened: "Reopened",
};

export const KIND_LABEL: Record<string, string> = {
  invoice: "Invoice", contract: "Contract rules", usage: "Usage events", payments: "Payments and credits", dispute: "Dispute statement", note: "Supporting note",
};

export const ACTION_LABEL: Record<string, string> = {
  credit: "Credit", rebill: "Additional charge", net_settlement: "Net settlement", no_adjustment: "No change", request_information: "Request information",
};

export const STEP_LABEL: Record<string, string> = {
  collect_evidence: "Collect evidence", check_missing_evidence: "Check for missing evidence", recalculate_invoice: "Recalculate invoice",
  ai_analysis: "Model analysis", fallback_analysis: "Fallback analysis", ground_and_validate: "Check citations and figures",
  price_resolution_options: "Price resolution options", save_analysis: "Save analysis",
};

export const PIPELINE = ["collect_evidence", "check_missing_evidence", "recalculate_invoice", "ai_analysis", "ground_and_validate", "price_resolution_options", "save_analysis"];

export function readingLabel(label: string): string {
  return label.replace(/_/g, " ");
}
