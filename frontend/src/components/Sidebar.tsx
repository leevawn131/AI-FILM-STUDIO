import React from 'react';
import {
  Film,
  Clapperboard,
  BookOpen,
  Layers,
  Users,
  MapPin,
  Send,
  FolderInput,
  Scissors,
  Music,
  Download,
  Coins,
  Sparkles,
  HardDrive
} from 'lucide-react';
import { Project } from '../api/client';

export type ViewTab =
  | 'dashboard'
  | 'projects'
  | 'screenplay'
  | 'storyboard'
  | 'characters'
  | 'locations'
  | 'flow_handoff'
  | 'media_import'
  | 'editor'
  | 'audio'
  | 'export'
  | 'cost';

interface SidebarProps {
  currentView: ViewTab;
  onSelectView: (view: ViewTab) => void;
  projects: Project[];
  activeProject: Project | null;
  onSelectProject: (project: Project) => void;
  onOpenNewProjectModal: () => void;
}

export const Sidebar: React.FC<SidebarProps> = ({
  currentView,
  onSelectView,
  projects,
  activeProject,
  onSelectProject,
  onOpenNewProjectModal,
}) => {
  const navItems = [
    { id: 'dashboard' as ViewTab, label: 'Dashboard', icon: Film, phase: 1 },
    { id: 'projects' as ViewTab, label: 'Projects', icon: HardDrive, phase: 1 },
    { id: 'screenplay' as ViewTab, label: 'Screenplay Studio', icon: BookOpen, phase: 2 },
    { id: 'storyboard' as ViewTab, label: 'Storyboard & Shots', icon: Layers, phase: 2 },
    { id: 'characters' as ViewTab, label: 'Character Database', icon: Users, phase: 3 },
    { id: 'locations' as ViewTab, label: 'Location Manager', icon: MapPin, phase: 3 },
    { id: 'flow_handoff' as ViewTab, label: 'Google Flow Handoff', icon: Send, phase: 4 },
    { id: 'media_import' as ViewTab, label: 'Media Library & Import', icon: FolderInput, phase: 4 },
    { id: 'editor' as ViewTab, label: 'Film Timeline & Cut', icon: Scissors, phase: 5 },
    { id: 'audio' as ViewTab, label: 'Audio & Voiceover', icon: Music, phase: 6 },
    { id: 'export' as ViewTab, label: 'Export Center', icon: Download, phase: 6 },
    { id: 'cost' as ViewTab, label: 'Cost & Budget Hub', icon: Coins, phase: 1 },
  ];

  return (
    <aside className="w-64 bg-studio-900 border-r border-studio-800 flex flex-col h-screen select-none">
      {/* Brand Header */}
      <div className="p-4 border-b border-studio-800 flex items-center space-x-3 bg-studio-950/60">
        <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-cinema-amber to-cinema-gold flex items-center justify-center shadow-lg shadow-cinema-amber/20">
          <Clapperboard className="w-5 h-5 text-studio-950" />
        </div>
        <div>
          <div className="flex items-center space-x-1.5">
            <span className="font-bold text-sm tracking-wide text-white uppercase">AI FILM STUDIO</span>
            <span className="text-[10px] bg-cinema-amber/20 text-cinema-gold px-1.5 py-0.5 rounded font-mono font-medium border border-cinema-amber/30">
              HYBRID
            </span>
          </div>
          <p className="text-[11px] text-slate-400 font-mono tracking-tight">Gemini • Flow • FFmpeg</p>
        </div>
      </div>

      {/* Active Project Switcher */}
      <div className="p-3 border-b border-studio-800 bg-studio-850/40">
        <div className="flex items-center justify-between mb-1.5">
          <span className="text-[10px] uppercase font-semibold text-slate-400 tracking-wider">Active Film</span>
          <button
            onClick={onOpenNewProjectModal}
            className="text-[11px] text-cinema-gold hover:text-white transition-colors flex items-center space-x-1"
          >
            <span>+ New</span>
          </button>
        </div>
        <select
          value={activeProject?.id || ''}
          onChange={(e) => {
            const found = projects.find((p) => p.id === e.target.value);
            if (found) onSelectProject(found);
          }}
          className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white focus:outline-none focus:border-cinema-gold truncate"
        >
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
          {projects.length === 0 && <option value="">No projects</option>}
        </select>
      </div>

      {/* Navigation Links */}
      <nav className="flex-1 overflow-y-auto p-2 space-y-1">
        {navItems.map((item) => {
          const Icon = item.icon;
          const isActive = currentView === item.id;
          return (
            <button
              key={item.id}
              onClick={() => onSelectView(item.id)}
              className={`w-full flex items-center justify-between px-3 py-2 rounded-lg text-xs font-medium transition-all group ${
                isActive
                  ? 'bg-studio-800 text-cinema-gold border-l-2 border-cinema-amber shadow-sm'
                  : 'text-slate-300 hover:bg-studio-800/60 hover:text-white'
              }`}
            >
              <div className="flex items-center space-x-2.5 truncate">
                <Icon
                  className={`w-4 h-4 transition-colors ${
                    isActive ? 'text-cinema-gold' : 'text-slate-400 group-hover:text-slate-200'
                  }`}
                />
                <span className="truncate">{item.label}</span>
              </div>
              <span className="text-[9px] font-mono text-slate-500 bg-studio-950 px-1.5 py-0.5 rounded">
                P{item.phase}
              </span>
            </button>
          );
        })}
      </nav>

      {/* Engine Status Footer */}
      <div className="p-3 border-t border-studio-800 bg-studio-950/70">
        <div className="flex items-center justify-between text-[11px] text-slate-400">
          <div className="flex items-center space-x-1.5">
            <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
            <span className="font-mono text-[10px]">Studio Engine: Ready</span>
          </div>
          <span className="text-[10px] text-cinema-gold font-mono">v1.0</span>
        </div>
        <div className="mt-1.5 flex items-center space-x-1 text-[10px] text-slate-500">
          <Sparkles className="w-3 h-3 text-cyan-400" />
          <span>Gemini & Offline Hybrid AI</span>
        </div>
      </div>
    </aside>
  );
};
