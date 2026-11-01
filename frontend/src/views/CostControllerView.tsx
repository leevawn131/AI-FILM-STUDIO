import React, { useEffect, useState } from 'react';
import { Project, BudgetSummary, fetchBudgetSummary, topUpBudget } from '../api/client';
import { Coins, DollarSign, PlusCircle, ShieldCheck, Hash, AlertTriangle, ArrowUpRight } from 'lucide-react';

interface CostControllerViewProps {
  activeProject: Project | null;
  onRefreshProject: () => void;
}

export const CostControllerView: React.FC<CostControllerViewProps> = ({
  activeProject,
  onRefreshProject,
}) => {
  const [summary, setSummary] = useState<BudgetSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [topUpAmount, setTopUpAmount] = useState(25);
  const [showTopUp, setShowTopUp] = useState(false);

  const loadData = async () => {
    if (!activeProject) return;
    setLoading(true);
    try {
      const data = await fetchBudgetSummary(activeProject.id);
      setSummary(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, [activeProject?.id]);

  const handleTopUp = async () => {
    if (!activeProject) return;
    try {
      await topUpBudget(activeProject.id, topUpAmount);
      setShowTopUp(false);
      await loadData();
      onRefreshProject();
    } catch (err) {
      alert('Top-up failed: ' + err);
    }
  };

  if (!activeProject) {
    return (
      <div className="p-8 text-center text-slate-400">
        Please select or create an active project to view cost and budget controls.
      </div>
    );
  }

  const budgetTotal = summary?.budget_total ?? activeProject.budget_total;
  const budgetSpent = summary?.budget_spent ?? activeProject.budget_spent;
  const budgetRemaining = summary?.budget_remaining ?? Math.max(0, budgetTotal - budgetSpent);
  const percentUsed = summary?.percent_used ?? (budgetTotal > 0 ? (budgetSpent / budgetTotal) * 100 : 0);

  return (
    <div className="p-8 space-y-8 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center space-x-2">
            <h2 className="text-xl font-bold text-white tracking-tight">Cost Controller & Budget Hub</h2>
            <span className="text-[10px] bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 px-2 py-0.5 rounded font-mono font-medium">
              MIN-EXPENSE MODE ACTIVE
            </span>
          </div>
          <p className="text-xs text-slate-400">
            Real-time credit tracking, duplicate generation prevention, and per-scene budget enforcement
          </p>
        </div>

        <button
          onClick={() => setShowTopUp(true)}
          className="bg-cinema-gold hover:bg-cinema-amber text-studio-950 px-4 py-2 rounded-lg text-xs font-bold flex items-center space-x-1.5 shadow-md shadow-cinema-amber/10 transition-all"
        >
          <PlusCircle className="w-4 h-4" />
          <span>Top-Up Budget</span>
        </button>
      </div>

      {/* Top-Up Modal */}
      {showTopUp && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-studio-900 border border-studio-700 rounded-xl p-6 max-w-sm w-full space-y-4 shadow-2xl">
            <h3 className="text-base font-bold text-white">Add Budget Credits</h3>
            <p className="text-xs text-slate-400">
              Increase the generation budget threshold for project "{activeProject.name}".
            </p>
            <div>
              <label className="block text-xs font-semibold text-slate-300 uppercase mb-1">
                Amount ($ USD)
              </label>
              <input
                type="number"
                min={5}
                step={5}
                value={topUpAmount}
                onChange={(e) => setTopUpAmount(Number(e.target.value))}
                className="w-full bg-studio-950 border border-studio-700 rounded-lg px-3 py-2 text-sm text-white font-mono focus:outline-none focus:border-cinema-gold"
              />
            </div>
            <div className="flex justify-end space-x-3 pt-2">
              <button
                onClick={() => setShowTopUp(false)}
                className="px-3 py-1.5 rounded text-xs text-slate-300 hover:text-white"
              >
                Cancel
              </button>
              <button
                onClick={handleTopUp}
                className="bg-cinema-gold text-studio-950 px-4 py-1.5 rounded font-bold text-xs"
              >
                Confirm +${topUpAmount}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* KPI Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-4 gap-4">
        <div className="p-5 rounded-xl bg-studio-900 border border-studio-800 space-y-1">
          <span className="text-[10px] font-mono uppercase text-slate-400">Total Budget Limit</span>
          <div className="text-2xl font-bold text-white font-mono">${budgetTotal.toFixed(2)}</div>
          <span className="text-[10px] text-slate-500 font-mono">Configurable ceiling</span>
        </div>

        <div className="p-5 rounded-xl bg-studio-900 border border-studio-800 space-y-1">
          <span className="text-[10px] font-mono uppercase text-slate-400">Budget Spent</span>
          <div className="text-2xl font-bold text-cinema-amber font-mono">${budgetSpent.toFixed(3)}</div>
          <span className="text-[10px] text-slate-500 font-mono">{summary?.total_actions || 0} studio actions charged</span>
        </div>

        <div className="p-5 rounded-xl bg-studio-900 border border-studio-800 space-y-1">
          <span className="text-[10px] font-mono uppercase text-slate-400">Remaining Balance</span>
          <div className="text-2xl font-bold text-emerald-400 font-mono">${budgetRemaining.toFixed(3)}</div>
          <span className="text-[10px] text-slate-500 font-mono">Available for workflows</span>
        </div>

        <div className="p-5 rounded-xl bg-studio-900 border border-studio-800 space-y-1">
          <span className="text-[10px] font-mono uppercase text-slate-400">Budget Consumption</span>
          <div className="text-2xl font-bold text-white font-mono">{percentUsed.toFixed(1)}%</div>
          <div className="w-full bg-studio-950 h-1.5 rounded-full overflow-hidden mt-1">
            <div
              className={`h-full ${percentUsed > 80 ? 'bg-cinema-crimson' : 'bg-emerald-400'}`}
              style={{ width: `${percentUsed}%` }}
            />
          </div>
        </div>
      </div>

      {/* Duplicate Prevention & Protection Banner */}
      <div className="p-5 rounded-xl bg-studio-900 border border-emerald-500/20 flex items-start space-x-4">
        <div className="w-9 h-9 rounded-lg bg-emerald-500/10 text-emerald-400 flex items-center justify-center shrink-0">
          <ShieldCheck className="w-5 h-5" />
        </div>
        <div className="space-y-1 text-xs">
          <h4 className="font-bold text-white">Cryptographic Duplicate Job Prevention Active</h4>
          <p className="text-slate-400">
            Every scene generation, Flow prompt formulation, and render request is hashed using SHA-256. If an identical request is submitted, the cached result is returned instantly with $0.00 redundant charge.
          </p>
        </div>
      </div>

      {/* Provider-Aware Architecture Breakdown */}
      <div className="p-6 rounded-xl bg-studio-900 border border-studio-800 space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-bold text-white uppercase tracking-wider">Provider & Architecture Cost Engine</h3>
          <span className="text-[10px] text-slate-400 font-mono">No vendor pricing assumptions • Transparent accounting</span>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
          <div className="p-3 bg-studio-950 rounded-lg border border-studio-800 text-left space-y-1">
            <div className="flex items-center justify-between">
              <span className="text-[10px] font-bold text-cinema-amber uppercase">Google Flow</span>
              <span className="text-[10px] bg-studio-800 text-slate-300 px-1.5 py-0.5 rounded font-mono">Credits</span>
            </div>
            <span className="text-xs font-bold text-white block">Flow Handoff Package</span>
            <span className="text-[10px] text-slate-400 block">Uses subscription credits. Direct API expense: $0.00</span>
          </div>

          <div className="p-3 bg-studio-950 rounded-lg border border-studio-800 text-left space-y-1">
            <div className="flex items-center justify-between">
              <span className="text-[10px] font-bold text-blue-400 uppercase">Google Gemini</span>
              <span className="text-[10px] bg-studio-800 text-slate-300 px-1.5 py-0.5 rounded font-mono">Tokens</span>
            </div>
            <span className="text-xs font-bold text-white block">Screenplay & Storyboard</span>
            <span className="text-[10px] text-slate-400 block">Tracks prompt & completion tokens. Distinguishes est. vs actual</span>
          </div>

          <div className="p-3 bg-studio-950 rounded-lg border border-studio-800 text-left space-y-1">
            <div className="flex items-center justify-between">
              <span className="text-[10px] font-bold text-emerald-400 uppercase">Local FFmpeg</span>
              <span className="text-[10px] bg-emerald-500/10 text-emerald-400 px-1.5 py-0.5 rounded font-mono">Local Host</span>
            </div>
            <span className="text-xs font-bold text-white block">Editing & Concatenation</span>
            <span className="text-[10px] text-slate-400 block">Zero cloud charges ($0.00). Unlimited local processing</span>
          </div>

          <div className="p-3 bg-studio-950 rounded-lg border border-studio-800 text-left space-y-1">
            <div className="flex items-center justify-between">
              <span className="text-[10px] font-bold text-purple-400 uppercase">Offline Engine</span>
              <span className="text-[10px] bg-purple-500/10 text-purple-400 px-1.5 py-0.5 rounded font-mono">On-Device</span>
            </div>
            <span className="text-xs font-bold text-white block">Audio & Synthesis</span>
            <span className="text-[10px] text-slate-400 block">No recurring API fees. Fallback synthesis is 100% free</span>
          </div>
        </div>
      </div>

      {/* Transaction Ledger Table */}
      <div className="p-6 rounded-xl bg-studio-900 border border-studio-800 space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-bold text-white uppercase tracking-wider">Provider-Aware Usage Ledger</h3>
          {summary?.total_flow_credits !== undefined && (
            <span className="text-xs font-mono text-cinema-gold">
              Total Flow Credits Used: <strong>{summary.total_flow_credits}</strong>
            </span>
          )}
        </div>
        {summary && summary.records.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="bg-studio-950 text-slate-400 uppercase font-mono text-[10px] border-b border-studio-800">
                <tr>
                  <th className="py-2.5 px-3">Timestamp</th>
                  <th className="py-2.5 px-3">Provider</th>
                  <th className="py-2.5 px-3">Operation</th>
                  <th className="py-2.5 px-3">Flow Credits</th>
                  <th className="py-2.5 px-3">Cost Accounting</th>
                  <th className="py-2.5 px-3">Tokens</th>
                  <th className="py-2.5 px-3">Details</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-studio-800">
                {summary.records.map((r) => {
                  const prov = r.provider || 'offline_engine';
                  const provColor =
                    prov === 'gemini'
                      ? 'text-blue-400 bg-blue-500/10 border-blue-500/30'
                      : prov === 'google_flow'
                      ? 'text-cinema-amber bg-cinema-amber/10 border-cinema-amber/30'
                      : prov === 'local_ffmpeg'
                      ? 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30'
                      : 'text-purple-400 bg-purple-500/10 border-purple-500/30';

                  const costDisplay = r.actual_cost_known && r.actual_cost !== null
                    ? `$${Number(r.actual_cost).toFixed(3)} (Actual)`
                    : r.cost_usd > 0
                    ? `$${Number(r.cost_usd).toFixed(3)} (Est.)`
                    : 'Free / Local';

                  const tokenCount = (r.input_tokens || 0) + (r.output_tokens || 0);

                  return (
                    <tr key={r.id} className="hover:bg-studio-850/50">
                      <td className="py-2.5 px-3 font-mono text-slate-400">
                        {new Date(r.created_at).toLocaleTimeString()}
                      </td>
                      <td className="py-2.5 px-3">
                        <span className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-mono border ${provColor}`}>
                          {prov.toUpperCase()}
                        </span>
                      </td>
                      <td className="py-2.5 px-3 font-semibold text-white">{r.operation || r.action_type}</td>
                      <td className="py-2.5 px-3 font-mono text-cinema-gold">
                        {(r.flow_credits || 0) > 0 ? `${r.flow_credits} Cr` : '-'}
                      </td>
                      <td className="py-2.5 px-3 font-mono text-emerald-400">{costDisplay}</td>
                      <td className="py-2.5 px-3 font-mono text-slate-400">
                        {tokenCount > 0 ? `${tokenCount.toLocaleString()} tkn` : '-'}
                      </td>
                      <td className="py-2.5 px-3 text-slate-300 font-mono text-[11px] truncate max-w-xs">{r.details}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="py-8 text-center text-slate-500 text-xs font-mono">
            No studio charges recorded yet. Workflows will appear here as you run Screenwriter, Storyboard, and Render operations.
          </div>
        )}
      </div>
    </div>
  );
};
