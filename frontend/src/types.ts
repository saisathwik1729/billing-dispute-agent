export type Citation = { ref: string; type: string; label: string; origin: "agent" | "engine" };

export type Finding = {
  id: string; seq: number; title: string; category: string; explanation: string; confidence: string;
  discrepancy_ids: string[]; citations: Citation[]; invalid_citations: string[]; unverified_numbers: string[];
  flags: string[]; source: string; status: "proposed" | "accepted" | "edited" | "rejected";
  reviewer_note: string | null; original: { title: string; explanation: string; category: string } | null;
  reviewed_by: string | null; reviewed_at: string | null; stale_reasons: string[];
};

export type OptionItem = { discrepancy_id: string; kind: string; gross: string; prior_credit: string; already_adjusted: string; net: string; note: string };
export type Computation = { amount: string; kind: "credit" | "debit" | "none"; items: OptionItem[]; choices: Record<string, string>; capped: boolean; warnings: string[]; currency?: string; flags?: string[] };

export type ResolutionOption = {
  id: string; seq: number; title: string; rationale: string; action: string; discrepancy_ids: string[];
  ambiguity_choices: Record<string, string>; citations: Citation[]; proposed: Computation; live: Computation | null;
  source: string; flags: string[]; unaccepted_discrepancies: string[];
};

export type MissingEvidence = { evidence_kind: string; reason: string; blocking: boolean; source: string };

export type Analysis = {
  id: string; run_id: string; source: "llm" | "fallback" | "hybrid"; created_at: string; provider: string; model: string;
  run_status: string; case_summary: string; claim_assessment: string; missing_evidence: MissingEvidence[];
  guardrail_report: any; calculation_id: string | null; stale: boolean; changed_kinds: string[];
  findings: Finding[]; options: ResolutionOption[];
};

export type Discrepancy = {
  id: string; kind: string; key: string; line_ids: string[]; rule_ids: string[]; event_ids: string[];
  ambiguity_ids: string[]; amount: string; range?: [string, string]; direction: string; description: string;
  causes: string[]; outcomes?: { choices: Record<string, string>; delta: string }[];
  prior_credit_applied: string; prior_credit_ids: string[]; workings?: string;
};

export type Ambiguity = { id: string; type: string; rule_ids: string[]; event_ids: string[]; description: string; options: { label: string; description: string }[] };

export type Calculation = {
  id: string; engine_version: string; input_hash: string; created_at: string; currency: string; invoice_id: string;
  period: { start: string; end: string; days: number }; discrepancies: Discrepancy[]; ambiguities: Ambiguity[];
  data_quality: { id: string; type: string; description: string; event_ids: string[] }[];
  unsupported_rules: { id: string; text: string }[]; totals: Record<string, string>; warnings: string[];
  prior_credits: { total: string; unattributed: string; allocations: { credit_id: string; discrepancy_id: string; amount: string }[] };
};

export type Evidence = { id: string; kind: string; filename: string; active: boolean; superseded_by: string | null; withdrawn_reason: string | null; warnings: string[]; added_by: string; created_at: string; summary: string };
export type Run = { id: string; status: string; provider: string; model: string; simulate_failures: string[]; error: string | null; triggered_by: string; created_at: string; finished_at: string | null; steps?: Step[] };
export type Step = { seq: number; name: string; status: string; summary: string; error: string | null; duration_ms: number; detail: any };
export type Adjustment = { id: string; kind: string; amount: string; computed_amount: string; currency: string; option_id: string; analysis_id: string; discrepancy_ids: string[]; note: string; approved_by: string; created_at: string };
export type InfoRequest = { id: string; evidence_kind: string; message: string; status: string; requested_by: string; created_at: string; fulfilled_by_evidence_id: string | null };

export type CaseSummary = {
  id: string; title: string; customer_name: string; status: string; dispute_description: string; sample_key: string | null;
  created_at: string; updated_at: string; resolution_summary: string | null; has_analysis?: boolean; stale?: boolean;
  confirmed_net_difference?: string | null; currency?: string | null; open_findings?: number; evidence_count?: number;
};

export type CaseDetail = {
  case: CaseSummary; evidence: Evidence[]; latest_analysis: Analysis | null;
  analyses: { id: string; created_at: string; source: string; stale: boolean }[];
  calculation: Calculation | null; runs: Run[]; active_run: Run | null; adjustments: Adjustment[];
  info_requests: InfoRequest[]; sample_followups: { index: number; kind: string; filename: string; label: string }[];
};

export type Sample = { key: string; title: string; customer_name: string; summary: string; files: { kind: string; filename: string }[]; followups: { index: number; kind: string; filename: string; label: string }[] };
