import { useEffect, useState } from "react";
import { api } from "../api";
import { money, readingLabel } from "../format";
import type { CaseDetail } from "../types";
import { Empty, ErrorBlock, Pill, Skeleton } from "./ui";

function qty(v: string | null | undefined): string {
  if (v === null || v === undefined || v === "") return "";
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString("en-US", { maximumFractionDigits: 2 }) : String(v);
}

function qtyDiffers(r: any): boolean {
  return r.invoiced_quantity != null && r.recalculated_quantity != null && Number(r.recalculated_quantity) !== Number(r.invoiced_quantity);
}

export default function InvoiceTab({ detail }: { detail: CaseDetail }) {
  const calc = detail.calculation;
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [cmp, setCmp] = useState<any>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!calc) return;
    let live = true;
    setLoading(true);
    api(`/api/cases/${detail.case.id}/comparison?choices=${encodeURIComponent(JSON.stringify(choices))}`)
      .then((r) => { if (live) { setCmp(r); setError(null); } })
      .catch((e) => live && setError(e))
      .finally(() => live && setLoading(false));
    return () => { live = false; };
  }, [detail.case.id, calc?.id, choices, detail.adjustments.length]);

  if (!calc) {
    return (
      <Empty title="Nothing to compare yet">
        <p>The comparison appears once an analysis has recalculated the invoice. That needs an invoice and the contract's pricing rules.</p>
      </Empty>
    );
  }
  const cur = calc.currency;
  const ambs = calc.ambiguities;

  return (
    <div className="invoice-tab">
      {ambs.length > 0 && (
        <section className="readings">
          <h2>Contract questions</h2>
          <p className="muted">The engine prices every reading. Pick one per question to see the invoice under it. Unanswered questions use the reading that keeps the invoice highest, so nothing is credited by default.</p>
          {ambs.map((a) => (
            <div key={a.id} className="reading">
              <div className="reading-q"><span className="ref">{a.id}</span> {a.description}</div>
              <div className="seg" role="radiogroup" aria-label={`Reading for ${a.id}`}>
                <button role="radio" aria-checked={!choices[a.id]} className={!choices[a.id] ? "is-on" : ""}
                  onClick={() => { const n = { ...choices }; delete n[a.id]; setChoices(n); }}>Unanswered</button>
                {a.options.map((o) => (
                  <button key={o.label} role="radio" aria-checked={choices[a.id] === o.label} title={o.description}
                    className={choices[a.id] === o.label ? "is-on" : ""} onClick={() => setChoices({ ...choices, [a.id]: o.label })}>
                    {readingLabel(o.label)}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </section>
      )}

      <ErrorBlock error={error} />
      {!cmp && <Skeleton lines={7} />}
      {cmp && (
        <div className={`sheet ${loading ? "is-loading" : ""}`} aria-label="Invoice with corrections">
          <div className="sheet-head">
            <div>
              <div className="sheet-title">Invoice {calc.invoice_id}</div>
              <div className="muted">{detail.case.customer_name}, service period {calc.period.start} to {calc.period.end}</div>
            </div>
            <div className="sheet-legend">
              <span><s className="pen-strike">0.00</s> as invoiced</span>
              <span><span className="pen">0.00</span> recalculated</span>
            </div>
          </div>
          <table className="lines">
            <thead>
              <tr><th scope="col">Line</th><th scope="col">Charge</th><th scope="col" className="num">Qty billed</th><th scope="col" className="num">Amount</th></tr>
            </thead>
            <tbody>
              {cmp.rows.map((r: any) => {
                const differs = r.recalculated !== null && r.difference !== "0.00";
                const missing = r.line_ids.length === 0;
                return (
                  <tr key={r.key} className={missing ? "row-added" : differs ? "row-diff" : r.recalculated === null ? "row-unsupported" : ""}>
                    <td className="lid">{r.line_ids.join(", ") || "new"}</td>
                    <td>
                      <div className="charge">{r.description}{missing && <span className="added-note"> not on the invoice</span>}</div>
                      <div className="workings">{r.workings} {r.rule_ids.map((id: string) => <span key={id} className="ref">{id}</span>)}</div>
                      {r.recalculated === null && <Pill tone="grey">No contract rule: kept as invoiced until evidence supports it</Pill>}
                    </td>
                    <td className="num">
                      {qtyDiffers(r) ? (
                        <><s className="pen-strike">{qty(r.invoiced_quantity)}</s><span className="pen">{qty(r.recalculated_quantity)}</span></>
                      ) : qty(r.invoiced_quantity ?? (missing ? null : ""))}
                    </td>
                    <td className="num amount-cell">
                      {missing ? <span className="pen">{money(r.recalculated)}</span> : differs ? (
                        <><s className="pen-strike">{money(r.invoiced)}</s><span className="pen">{money(r.recalculated)}</span></>
                      ) : money(r.invoiced)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
            <tfoot>
              <tr><td /><th scope="row">Invoice total</th><td /><td className="num">
                {cmp.difference !== "0.00" ? <><s className="pen-strike">{money(cmp.invoiced_total)}</s><span className="pen">{money(cmp.recalculated_total)}</span></> : money(cmp.invoiced_total)}
              </td></tr>
              <tr className="sum-sub"><td /><th scope="row">Difference under these readings</th><td /><td className={`num ${Number(cmp.difference) > 0 ? "red" : ""}`}>{money(cmp.difference, cur)}</td></tr>
              <tr className="sum-sub"><td /><th scope="row">Credits already in the payment history</th><td /><td className="num">{money(cmp.prior_credits_total)}</td></tr>
              <tr className="sum-sub"><td /><th scope="row">Adjustments approved in this case</th><td /><td className="num">{money(cmp.approved_adjustments.reduce((s: number, a: any) => s + Number(a.amount), 0))}</td></tr>
              <tr className="sum-final"><td /><th scope="row">Customer's net charge after credits</th><td /><td className="num">{money(cmp.net_after_credits, cur)}</td></tr>
              <tr className="sum-sub"><td /><th scope="row">Still to resolve against these readings</th><td /><td className="num">{money(cmp.remaining_difference, cur)}</td></tr>
            </tfoot>
          </table>
          {cmp.stale && <p className="flag flag-stale">This comparison is based on an analysis whose evidence has since changed.</p>}
        </div>
      )}

      <section className="discrepancies">
        <h2>What the engine found</h2>
        {calc.discrepancies.length === 0 && <p className="muted">The invoice matches the contract and usage on file.</p>}
        {calc.discrepancies.map((d) => (
          <div key={d.id} className={`disc disc-${d.kind}`}>
            <div className="disc-top">
              <span className="ref">{d.id}</span>
              <Pill tone={d.kind === "calculation_error" ? "red" : d.kind === "interpretation_dependent" ? "amber" : "grey"}>
                {d.kind === "calculation_error" ? "Calculation error" : d.kind === "interpretation_dependent" ? "Depends on interpretation" : "Needs evidence"}
              </Pill>
              <strong className="disc-amt">{d.kind === "interpretation_dependent" ? `${money(d.range![0])} to ${money(d.range![1])}` : money(d.amount, cur)}</strong>
            </div>
            <p>{d.description}</p>
            {d.causes.map((c, i) => <p key={i} className="cause">{c}</p>)}
            {Number(d.prior_credit_applied) > 0 && <p className="cause">Already credited {money(d.prior_credit_applied)} by {d.prior_credit_ids.join(", ")}.</p>}
            {d.outcomes && (
              <table className="outcomes">
                <thead><tr>{d.ambiguity_ids.map((a) => <th key={a}>{a}</th>)}<th className="num">Overcharge (+) / undercharge (−)</th></tr></thead>
                <tbody>{d.outcomes.map((o, i) => (
                  <tr key={i}>{d.ambiguity_ids.map((a) => <td key={a}>{readingLabel(o.choices[a] || "")}</td>)}<td className="num">{money(o.delta)}</td></tr>
                ))}</tbody>
              </table>
            )}
          </div>
        ))}
        {calc.data_quality.length > 0 && <h3 className="sub">Usage data notes</h3>}
        {calc.data_quality.map((q) => <p key={q.id} className="cause"><span className="ref">{q.id}</span> {q.description}</p>)}
        {calc.unsupported_rules.length > 0 && <h3 className="sub">Clauses the engine cannot price</h3>}
        {calc.unsupported_rules.map((r) => <p key={r.id} className="cause"><span className="ref">{r.id}</span> {r.text}</p>)}
        {calc.warnings.map((w, i) => <p key={i} className="flag">{w}</p>)}
        <p className="muted small">Engine {calc.engine_version}, calculation {calc.id}, input fingerprint {calc.input_hash.slice(0, 12)}. Amounts use decimal arithmetic rounded half-up per line; tax is out of scope.</p>
      </section>
    </div>
  );
}
