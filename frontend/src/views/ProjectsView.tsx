import React, { useState } from 'react';
import { Project, deleteProject } from '../api/client';
import { Film, Plus, Trash2, CheckCircle2, Clock, DollarSign, Layers } from 'lucide-react';

interface ProjectsViewProps {
  projects: Project[];
  activeProject: Project | null;
  onSelectProject: (p: Project) => void;
  onRefresh: () => void;
  onOpenNewProjectModal: () => void;
}

export const ProjectsView: React.FC<ProjectsViewProps> = ({
  projects,
  activeProject,
  onSelectProject,
  onRefresh,
  onOpenNewProjectModal,
}) => {
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const handleDelete = async (id: string, name: string) => {
    if (!window.confirm(`Are you sure you want to delete project "${name}" and its files?`)) {
      return;
    }
    setDeletingId(id);
    try {
      await deleteProject(id);
      onRefresh();
    } catch (err) {
      alert('Failed to delete project: ' + err);
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <div className="p-8 space-y-6 max-w-7xl mx-auto">
      {/* View Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-bold text-white tracking-tight">Film Productions</h2>
          <p className="text-xs text-slate-400">Manage all AI film projects in your local SQLite studio database</p>
        </div>
        <button
          onClick={onOpenNewProjectModal}
          className="bg-cinema-gold hover:bg-cinema-amber text-studio-950 px-4 py-2 rounded-lg text-xs font-bold flex items-center space-x-1.5 shadow-md shadow-cinema-amber/10 transition-all active:scale-95"
        >
          <Plus className="w-4 h-4 stroke-[2.5]" />
          <span>New Project</span>
        </button>
      </div>

      {/* Projects Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {projects.map((p) => {
          const isActive = activeProject?.id === p.id;
          const budgetRem = Math.max(0, p.budget_total - p.budget_spent);

          return (
            <div
              key={p.id}
              className={`rounded-xl border transition-all flex flex-col justify-between overflow-hidden ${
                isActive
                  ? 'bg-studio-900 border-cinema-amber shadow-xl shadow-cinema-amber/5 ring-1 ring-cinema-amber/20'
                  : 'bg-studio-900/80 border-studio-800 hover:border-studio-700'
              }`}
            >
              {/* Card Header */}
              <div className="p-5 space-y-3">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] font-mono font-semibold px-2 py-0.5 rounded bg-studio-950 text-cyan-400 border border-cyan-800/30">
                    {p.genre}
                  </span>
                  <div className="flex items-center space-x-1 text-[10px] text-slate-400 font-mono">
                    <span>{p.aspect_ratio}</span>
                    <span>•</span>
                    <span>{p.fps}fps</span>
                  </div>
                </div>

                <div>
                  <h3 className="text-base font-bold text-white tracking-tight">{p.name}</h3>
                  <p className="text-xs text-slate-400 line-clamp-2 mt-1">
                    {p.logline || 'No logline defined.'}
                  </p>
                </div>

                {/* Visual Style snippet */}
                <div className="bg-studio-950 p-2.5 rounded-lg border border-studio-800/80">
                  <span className="text-[9px] uppercase font-mono text-slate-400 block mb-0.5">Flow Visual Style</span>
                  <p className="text-[11px] text-slate-300 font-mono line-clamp-2">{p.visual_style}</p>
                </div>

                {/* Quick stats grid */}
                <div className="grid grid-cols-3 gap-2 pt-1 text-center">
                  <div className="bg-studio-850 p-2 rounded">
                    <span className="text-[10px] text-slate-400 block">Scenes</span>
                    <span className="text-sm font-bold text-white font-mono">{p.scenes_count || 0}</span>
                  </div>
                  <div className="bg-studio-850 p-2 rounded">
                    <span className="text-[10px] text-slate-400 block">Shots</span>
                    <span className="text-sm font-bold text-white font-mono">{p.shots_count || 0}</span>
                  </div>
                  <div className="bg-studio-850 p-2 rounded">
                    <span className="text-[10px] text-slate-400 block">Budget Left</span>
                    <span className="text-sm font-bold text-emerald-400 font-mono">${budgetRem.toFixed(0)}</span>
                  </div>
                </div>
              </div>

              {/* Card Footer Actions */}
              <div className="px-5 py-3 bg-studio-950/60 border-t border-studio-800 flex items-center justify-between">
                {isActive ? (
                  <span className="text-xs text-cinema-gold font-bold flex items-center space-x-1.5">
                    <CheckCircle2 className="w-4 h-4 text-cinema-gold" />
                    <span>Active Production</span>
                  </span>
                ) : (
                  <button
                    onClick={() => onSelectProject(p)}
                    className="text-xs text-slate-300 hover:text-cinema-gold font-semibold transition-colors"
                  >
                    Select as Active
                  </button>
                )}

                <button
                  onClick={() => handleDelete(p.id, p.name)}
                  disabled={deletingId === p.id}
                  className="text-slate-500 hover:text-cinema-crimson transition-colors p-1"
                  title="Delete project"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
