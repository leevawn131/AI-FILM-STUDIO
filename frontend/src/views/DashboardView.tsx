import React from 'react';
import {
  Film,
  BookOpen,
  Layers,
  Send,
  FolderInput,
  Scissors,
  Download,
  Coins,
  Sparkles,
  ArrowRight,
  CheckCircle2,
  Clock,
  Play
} from 'lucide-react';
import { Project, DashboardStats } from '../api/client';
import { ViewTab } from '../components/Sidebar';

interface DashboardViewProps {
  activeProject: Project | null;
  projects: Project[];
  stats: DashboardStats | null;
  onSelectView: (view: ViewTab) => void;
  onSelectProject: (project: Project) => void;
  onOpenNewProjectModal: () => void;
}

export const DashboardView: React.FC<DashboardViewProps> = ({
  activeProject,
  projects,
  stats,
  onSelectView,
  onSelectProject,
  onOpenNewProjectModal,
}) => {
  const scenesCount = activeProject?.scenes_count || 0;
  const shotsCount = activeProject?.shots_count || 0;
  const readyShots = activeProject?.ready_shots_count || 0;
  const budgetTotal = activeProject?.budget_total || 100;
  const budgetSpent = activeProject?.budget_spent || 0;
  const percentReady = shotsCount > 0 ? Math.round((readyShots / shotsCount) * 100) : 0;

  const pipelineStages = [
    { title: 'Screenplay', desc: 'Scenes & Dialogue', view: 'screenplay' as ViewTab, done: scenesCount > 0 },
    { title: 'Storyboard', desc: 'Shots & Flow Prompts', view: 'storyboard' as ViewTab, done: shotsCount > 0 },
    { title: 'Flow Handoff', desc: 'Prompt Package', view: 'flow_handoff' as ViewTab, done: shotsCount > 0 },
    { title: 'Media Import', desc: 'Folder Auto-Match', view: 'media_import' as ViewTab, done: readyShots > 0 },
    { title: 'Film Editor', desc: 'Timeline & Cut', view: 'editor' as ViewTab, done: readyShots > 0 },
    { title: 'Master Export', desc: 'Render MP4', view: 'export' as ViewTab, done: false },
  ];

  return (
    <div className="p-8 space-y-8 max-w-7xl mx-auto">
      {/* Hero Active Film Card */}
      {activeProject ? (
        <div className="relative overflow-hidden rounded-2xl bg-gradient-to-r from-studio-900 via-studio-850 to-studio-900 border border-studio-700/80 p-8 shadow-2xl">
          <div className="absolute top-0 right-0 w-96 h-96 bg-cinema-amber/5 rounded-full blur-3xl pointer-events-none" />
          <div className="relative z-10 space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs bg-cinema-amber/15 text-cinema-gold px-3 py-1 rounded-full border border-cinema-amber/30 font-semibold uppercase tracking-wider">
                Active Production
              </span>
              <span className="text-xs bg-studio-800 text-cyan-400 px-3 py-1 rounded-full border border-cyan-500/20 font-mono">
                {activeProject.genre}
              </span>
              <span className="text-xs bg-studio-800 text-slate-300 px-3 py-1 rounded-full border border-studio-700 font-mono">
                {activeProject.aspect_ratio} Anamorphic • {activeProject.fps} FPS
              </span>
            </div>

            <div className="max-w-3xl space-y-2">
              <h1 className="text-3xl font-extrabold text-white tracking-tight sm:text-4xl">
                {activeProject.name}
              </h1>
              <p className="text-sm text-slate-300 leading-relaxed">
                {activeProject.logline || 'A high-concept cinematic production powered by Google Gemini, Google Flow, and FFmpeg.'}
              </p>
            </div>

            <div className="pt-2 flex flex-wrap items-center gap-3">
              <button
                onClick={() => onSelectView('screenplay')}
                className="bg-cinema-gold hover:bg-cinema-amber text-studio-950 px-5 py-2.5 rounded-xl font-bold text-xs flex items-center space-x-2 shadow-lg shadow-cinema-amber/15 transition-all active:scale-95"
              >
                <BookOpen className="w-4 h-4" />
                <span>Open Screenplay Studio</span>
              </button>

              <button
                onClick={() => onSelectView('storyboard')}
                className="bg-studio-800 hover:bg-studio-700 text-white px-5 py-2.5 rounded-xl font-semibold text-xs border border-studio-600 flex items-center space-x-2 transition-all"
              >
                <Layers className="w-4 h-4 text-cinema-gold" />
                <span>View Storyboard ({shotsCount} shots)</span>
              </button>

              <button
                onClick={() => onSelectView('flow_handoff')}
                className="bg-studio-800 hover:bg-studio-700 text-cyan-300 px-5 py-2.5 rounded-xl font-semibold text-xs border border-cyan-800/40 flex items-center space-x-2 transition-all"
              >
                <Send className="w-4 h-4" />
                <span>Google Flow Package</span>
              </button>
            </div>
          </div>
        </div>
      ) : (
        <div className="p-8 rounded-2xl bg-studio-900 border border-dashed border-studio-700 text-center space-y-4">
          <Film className="w-12 h-12 text-slate-500 mx-auto" />
          <h2 className="text-lg font-bold text-white">No Film Projects Found</h2>
          <p className="text-xs text-slate-400 max-w-md mx-auto">
            Create your first film production to start generating screenplays, storyboards, and Flow video prompt packages.
          </p>
          <button
            onClick={onOpenNewProjectModal}
            className="bg-cinema-gold text-studio-950 px-5 py-2 rounded-lg font-bold text-xs"
          >
            Create New Film
          </button>
        </div>
      )}

      {/* KPI Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="p-5 rounded-xl bg-studio-900 border border-studio-800 space-y-2">
          <div className="flex items-center justify-between text-slate-400">
            <span className="text-xs font-semibold uppercase tracking-wider">Screenplay Scenes</span>
            <BookOpen className="w-4 h-4 text-cinema-gold" />
          </div>
          <div className="flex items-baseline space-x-2">
            <span className="text-2xl font-extrabold text-white font-mono">{scenesCount}</span>
            <span className="text-xs text-slate-400">scenes written</span>
          </div>
          <p className="text-[11px] text-slate-500 font-mono">Ready for director breakdown</p>
        </div>

        <div className="p-5 rounded-xl bg-studio-900 border border-studio-800 space-y-2">
          <div className="flex items-center justify-between text-slate-400">
            <span className="text-xs font-semibold uppercase tracking-wider">Storyboard Shots</span>
            <Layers className="w-4 h-4 text-cyan-400" />
          </div>
          <div className="flex items-baseline space-x-2">
            <span className="text-2xl font-extrabold text-white font-mono">{shotsCount}</span>
            <span className="text-xs text-slate-400">camera shots</span>
          </div>
          <p className="text-[11px] text-slate-500 font-mono">Flow video prompts generated</p>
        </div>

        <div className="p-5 rounded-xl bg-studio-900 border border-studio-800 space-y-2">
          <div className="flex items-center justify-between text-slate-400">
            <span className="text-xs font-semibold uppercase tracking-wider">Footage Ingested</span>
            <FolderInput className="w-4 h-4 text-emerald-400" />
          </div>
          <div className="flex items-baseline space-x-2">
            <span className="text-2xl font-extrabold text-white font-mono">{readyShots} / {shotsCount || 0}</span>
            <span className="text-xs text-emerald-400 font-mono">({percentReady}%)</span>
          </div>
          <div className="w-full bg-studio-950 h-1.5 rounded-full overflow-hidden">
            <div className="bg-emerald-400 h-full transition-all duration-300" style={{ width: `${percentReady}%` }} />
          </div>
        </div>

        <div className="p-5 rounded-xl bg-studio-900 border border-studio-800 space-y-2">
          <div className="flex items-center justify-between text-slate-400">
            <span className="text-xs font-semibold uppercase tracking-wider">Budget Balance</span>
            <Coins className="w-4 h-4 text-cinema-amber" />
          </div>
          <div className="flex items-baseline space-x-2">
            <span className="text-2xl font-extrabold text-white font-mono">
              ${(budgetTotal - budgetSpent).toFixed(2)}
            </span>
            <span className="text-xs text-slate-400 font-mono">/ ${budgetTotal.toFixed(0)}</span>
          </div>
          <p className="text-[11px] text-slate-500 font-mono">Spent: ${budgetSpent.toFixed(3)} (Min API Mode)</p>
        </div>
      </div>

      {/* Filmmaking Pipeline Stepper */}
      <div className="p-6 rounded-xl bg-studio-900 border border-studio-800 space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-sm font-bold text-white uppercase tracking-wider">Filmmaking Workflow Pipeline</h3>
            <p className="text-xs text-slate-400">From screenplay to Google Flow handoff and automatic FFmpeg editing</p>
          </div>
          <span className="text-xs text-cinema-gold font-mono font-medium">Hybrid Architecture</span>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
          {pipelineStages.map((stage, idx) => (
            <button
              key={stage.title}
              onClick={() => onSelectView(stage.view)}
              className={`p-3 rounded-lg border text-left transition-all group ${
                stage.done
                  ? 'bg-studio-850 border-emerald-500/30 hover:border-emerald-500/60'
                  : 'bg-studio-950/60 border-studio-800 hover:border-studio-700'
              }`}
            >
              <div className="flex items-center justify-between mb-1.5">
                <span className="text-[10px] font-mono text-slate-400">Step 0{idx + 1}</span>
                {stage.done ? (
                  <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
                ) : (
                  <Clock className="w-3.5 h-3.5 text-slate-500" />
                )}
              </div>
              <h4 className="text-xs font-bold text-white group-hover:text-cinema-gold transition-colors">
                {stage.title}
              </h4>
              <p className="text-[10px] text-slate-400 truncate mt-0.5">{stage.desc}</p>
            </button>
          ))}
        </div>
      </div>

      {/* Production Quick Action Launchpad */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <button
          onClick={() => onSelectView('screenplay')}
          className="p-5 rounded-xl bg-studio-900 border border-studio-800 hover:border-cinema-gold/50 text-left transition-all group flex flex-col justify-between space-y-3"
        >
          <div className="space-y-1">
            <div className="w-9 h-9 rounded-lg bg-cinema-amber/15 text-cinema-gold flex items-center justify-center mb-2">
              <BookOpen className="w-4 h-4" />
            </div>
            <h4 className="text-sm font-bold text-white group-hover:text-cinema-gold transition-colors">
              AI Screenwriter Agent
            </h4>
            <p className="text-xs text-slate-400">
              Interactive screenplay authoring with sluglines, dialogues, and Fountain formatting.
            </p>
          </div>
          <div className="flex items-center text-xs text-cinema-gold font-medium pt-2">
            <span>Write Scene</span>
            <ArrowRight className="w-3.5 h-3.5 ml-1.5 group-hover:translate-x-1 transition-transform" />
          </div>
        </button>

        <button
          onClick={() => onSelectView('flow_handoff')}
          className="p-5 rounded-xl bg-studio-900 border border-studio-800 hover:border-cyan-500/50 text-left transition-all group flex flex-col justify-between space-y-3"
        >
          <div className="space-y-1">
            <div className="w-9 h-9 rounded-lg bg-cyan-500/15 text-cyan-400 flex items-center justify-center mb-2">
              <Send className="w-4 h-4" />
            </div>
            <h4 className="text-sm font-bold text-white group-hover:text-cyan-400 transition-colors">
              Google Flow Handoff
            </h4>
            <p className="text-xs text-slate-400">
              Generate camera prompts, negative prompts, character anchor sheets, and downloadable Flow packages.
            </p>
          </div>
          <div className="flex items-center text-xs text-cyan-400 font-medium pt-2">
            <span>Export Flow Package</span>
            <ArrowRight className="w-3.5 h-3.5 ml-1.5 group-hover:translate-x-1 transition-transform" />
          </div>
        </button>

        <button
          onClick={() => onSelectView('editor')}
          className="p-5 rounded-xl bg-studio-900 border border-studio-800 hover:border-purple-500/50 text-left transition-all group flex flex-col justify-between space-y-3"
        >
          <div className="space-y-1">
            <div className="w-9 h-9 rounded-lg bg-purple-500/15 text-purple-400 flex items-center justify-center mb-2">
              <Scissors className="w-4 h-4" />
            </div>
            <h4 className="text-sm font-bold text-white group-hover:text-purple-400 transition-colors">
              FFmpeg Automatic Film Editor
            </h4>
            <p className="text-xs text-slate-400">
              Stitch imported clips, align audio tracks, overlay cinematic subtitles, and export master MP4s.
            </p>
          </div>
          <div className="flex items-center text-xs text-purple-400 font-medium pt-2">
            <span>Open Timeline Cut</span>
            <ArrowRight className="w-3.5 h-3.5 ml-1.5 group-hover:translate-x-1 transition-transform" />
          </div>
        </button>
      </div>

      {/* Projects Showcase Table */}
      <div className="p-6 rounded-xl bg-studio-900 border border-studio-800 space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-sm font-bold text-white uppercase tracking-wider">Studio Project Catalog</h3>
            <p className="text-xs text-slate-400">Manage all film productions in your local studio database</p>
          </div>
          <button
            onClick={() => onSelectView('projects')}
            className="text-xs text-cinema-gold hover:underline font-semibold"
          >
            Manage All Projects →
          </button>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead className="bg-studio-950 text-slate-400 uppercase font-mono text-[10px] border-b border-studio-800">
              <tr>
                <th className="py-2.5 px-3">Title</th>
                <th className="py-2.5 px-3">Genre</th>
                <th className="py-2.5 px-3">Aspect Ratio</th>
                <th className="py-2.5 px-3">Scenes</th>
                <th className="py-2.5 px-3">Shots</th>
                <th className="py-2.5 px-3">Budget Remaining</th>
                <th className="py-2.5 px-3 text-right">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-studio-800">
              {projects.map((p) => {
                const isActive = activeProject?.id === p.id;
                return (
                  <tr
                    key={p.id}
                    className={`hover:bg-studio-850/50 transition-colors ${isActive ? 'bg-studio-850/30' : ''}`}
                  >
                    <td className="py-3 px-3 font-semibold text-white flex items-center space-x-2">
                      {isActive && <span className="w-1.5 h-1.5 rounded-full bg-cinema-gold" />}
                      <span>{p.name}</span>
                    </td>
                    <td className="py-3 px-3 text-cyan-400 font-mono">{p.genre}</td>
                    <td className="py-3 px-3 text-slate-300 font-mono">{p.aspect_ratio}</td>
                    <td className="py-3 px-3 text-slate-300 font-mono">{p.scenes_count || 0}</td>
                    <td className="py-3 px-3 text-slate-300 font-mono">{p.shots_count || 0}</td>
                    <td className="py-3 px-3 text-emerald-400 font-mono">
                      ${Math.max(0, p.budget_total - p.budget_spent).toFixed(2)}
                    </td>
                    <td className="py-3 px-3 text-right">
                      {isActive ? (
                        <span className="text-[10px] bg-cinema-amber/15 text-cinema-gold px-2 py-0.5 rounded font-mono font-bold">
                          CURRENT
                        </span>
                      ) : (
                        <button
                          onClick={() => onSelectProject(p)}
                          className="text-[11px] text-slate-400 hover:text-cinema-gold font-medium"
                        >
                          Select
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
};
