import React from 'react';
import { Project } from '../api/client';
import { Film, DollarSign, Plus, Eye, Monitor } from 'lucide-react';

interface HeaderProps {
  activeProject: Project | null;
  onOpenNewProjectModal: () => void;
  onSelectView: (view: any) => void;
}

export const Header: React.FC<HeaderProps> = ({
  activeProject,
  onOpenNewProjectModal,
  onSelectView,
}) => {
  const budgetTotal = activeProject?.budget_total || 100;
  const budgetSpent = activeProject?.budget_spent || 0;
  const budgetRemaining = Math.max(0, budgetTotal - budgetSpent);
  const percentSpent = Math.min(100, Math.round((budgetSpent / budgetTotal) * 100));

  return (
    <header className="h-16 bg-studio-900 border-b border-studio-800 px-6 flex items-center justify-between">
      {/* Left: Active Project Info */}
      <div className="flex items-center space-x-4">
        {activeProject ? (
          <div>
            <div className="flex items-center space-x-2.5">
              <h1 className="text-base font-bold text-white tracking-tight">{activeProject.name}</h1>
              <span className="text-[10px] bg-studio-800 text-cinema-cyan px-2 py-0.5 rounded border border-cinema-cyan/20 font-mono font-medium">
                {activeProject.genre}
              </span>
              <span className="text-[10px] bg-studio-800 text-slate-300 px-2 py-0.5 rounded border border-studio-700 font-mono">
                {activeProject.aspect_ratio} Anamorphic
              </span>
              <span className="text-[10px] bg-studio-800 text-slate-300 px-2 py-0.5 rounded border border-studio-700 font-mono">
                {activeProject.fps} FPS
              </span>
            </div>
            <p className="text-xs text-slate-400 truncate max-w-xl mt-0.5 font-normal">
              {activeProject.logline || 'No logline defined yet.'}
            </p>
          </div>
        ) : (
          <div className="text-sm text-slate-400 flex items-center space-x-2">
            <Film className="w-4 h-4 text-slate-500" />
            <span>No project selected. Create or select a project to begin.</span>
          </div>
        )}
      </div>

      {/* Right: Budget Widget & Actions */}
      <div className="flex items-center space-x-4">
        {activeProject && (
          <button
            onClick={() => onSelectView('cost')}
            className="flex items-center space-x-3 bg-studio-950 border border-studio-800 hover:border-cinema-gold/40 px-3.5 py-1.5 rounded-lg transition-all text-left group"
          >
            <div className="w-7 h-7 rounded bg-studio-850 flex items-center justify-center text-cinema-gold group-hover:scale-105 transition-transform">
              <DollarSign className="w-4 h-4" />
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <span className="text-[10px] uppercase font-mono text-slate-400">Budget Tracker</span>
                <span className="text-[10px] font-mono font-semibold text-emerald-400">
                  ${budgetRemaining.toFixed(2)} left
                </span>
              </div>
              <div className="w-28 bg-studio-800 h-1.5 rounded-full overflow-hidden mt-1">
                <div
                  className={`h-full transition-all duration-300 ${
                    percentSpent > 85 ? 'bg-cinema-crimson' : percentSpent > 60 ? 'bg-cinema-amber' : 'bg-emerald-400'
                  }`}
                  style={{ width: `${percentSpent}%` }}
                />
              </div>
            </div>
          </button>
        )}

        <button
          onClick={onOpenNewProjectModal}
          className="flex items-center space-x-1.5 bg-gradient-to-r from-cinema-amber to-cinema-gold hover:opacity-95 text-studio-950 px-3.5 py-2 rounded-lg text-xs font-semibold shadow-md shadow-cinema-amber/10 transition-all active:scale-95"
        >
          <Plus className="w-4 h-4 stroke-[2.5]" />
          <span>New Film</span>
        </button>
      </div>
    </header>
  );
};
