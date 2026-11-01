import React, { useState, useEffect } from 'react';
import {
  Project,
  FlowPackageInfo,
  fetchFlowPackageInfo,
  getFlowPackageDownloadUrl,
} from '../api/client';
import {
  Send,
  Download,
  Copy,
  Check,
  FileText,
  Clock,
  Layers,
  Users,
  ShieldCheck,
  FolderInput,
  Sparkles
} from 'lucide-react';
import { ViewTab } from '../components/Sidebar';

interface FlowHandoffViewProps {
  activeProject: Project | null;
  onSelectView: (view: ViewTab) => void;
}

export const FlowHandoffView: React.FC<FlowHandoffViewProps> = ({
  activeProject,
  onSelectView,
}) => {
  const [pkgInfo, setPkgInfo] = useState<FlowPackageInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [copiedAll, setCopiedAll] = useState(false);
  const [activeTab, setActiveTab] = useState<'instructions' | 'prompts'>('prompts');

  const loadInfo = async () => {
    if (!activeProject) return;
    setLoading(true);
    try {
      const data = await fetchFlowPackageInfo(activeProject.id);
      setPkgInfo(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadInfo();
  }, [activeProject?.id]);

  const handleCopyAll = () => {
    if (!pkgInfo) return;
    navigator.clipboard.writeText(pkgInfo.quick_copy_txt);
    setCopiedAll(true);
    setTimeout(() => setCopiedAll(false), 2000);
  };

  if (!activeProject) {
    return <div className="p-8 text-center text-slate-400">Please select a project first.</div>;
  }

  return (
    <div className="p-8 space-y-6 max-w-7xl mx-auto">
      {/* View Header */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="flex items-center space-x-2">
            <h2 className="text-xl font-bold text-white tracking-tight">Google Flow Handoff Generator</h2>
            <span className="text-[10px] bg-cyan-500/10 text-cyan-400 border border-cyan-500/30 px-2 py-0.5 rounded font-mono font-medium">
              Production Handoff
            </span>
          </div>
          <p className="text-xs text-slate-400">
            Generate prompt sheets, character reference packages, and downloadable production instructions for Google Flow
          </p>
        </div>

        <div className="flex items-center space-x-3">
          <button
            onClick={() => onSelectView('media_import')}
            className="bg-studio-900 hover:bg-studio-800 text-slate-200 border border-studio-700 px-3.5 py-2 rounded-lg text-xs font-semibold flex items-center space-x-1.5 transition-all shadow"
          >
            <FolderInput className="w-4 h-4 text-emerald-400" />
            <span>Open Media Watcher →</span>
          </button>

          <a
            href={getFlowPackageDownloadUrl(activeProject.id)}
            download
            className="bg-gradient-to-r from-cyan-600 to-blue-600 hover:opacity-95 text-white px-4 py-2 rounded-lg text-xs font-bold flex items-center space-x-2 shadow-lg shadow-cyan-900/20 transition-all active:scale-95"
          >
            <Download className="w-4 h-4" />
            <span>Download ZIP Package</span>
          </a>
        </div>
      </div>

      {/* Package Stats Overview */}
      <div className="grid grid-cols-1 sm:grid-cols-4 gap-4">
        <div className="p-4 rounded-xl bg-studio-900 border border-studio-800 space-y-1">
          <div className="flex items-center justify-between text-slate-400">
            <span className="text-[10px] font-mono uppercase">Storyboard Shots</span>
            <Layers className="w-4 h-4 text-cinema-gold" />
          </div>
          <div className="text-2xl font-bold text-white font-mono">{pkgInfo?.total_shots || 0}</div>
          <span className="text-[10px] text-slate-500 font-mono">Flow video prompts formatted</span>
        </div>

        <div className="p-4 rounded-xl bg-studio-900 border border-studio-800 space-y-1">
          <div className="flex items-center justify-between text-slate-400">
            <span className="text-[10px] font-mono uppercase">Character Anchors</span>
            <Users className="w-4 h-4 text-cyan-400" />
          </div>
          <div className="text-2xl font-bold text-white font-mono">{pkgInfo?.total_characters || 0}</div>
          <span className="text-[10px] text-slate-500 font-mono">Costume & facial traits locked</span>
        </div>

        <div className="p-4 rounded-xl bg-studio-900 border border-studio-800 space-y-1">
          <div className="flex items-center justify-between text-slate-400">
            <span className="text-[10px] font-mono uppercase">Estimated Footage</span>
            <Clock className="w-4 h-4 text-emerald-400" />
          </div>
          <div className="text-2xl font-bold text-white font-mono">{pkgInfo?.estimated_minutes || 0}m</div>
          <span className="text-[10px] text-slate-500 font-mono">Total sequence duration</span>
        </div>

        <div className="p-4 rounded-xl bg-studio-900 border border-studio-800 space-y-1">
          <div className="flex items-center justify-between text-slate-400">
            <span className="text-[10px] font-mono uppercase">Handoff Mode</span>
            <ShieldCheck className="w-4 h-4 text-purple-400" />
          </div>
          <div className="text-sm font-bold text-emerald-400 font-mono pt-1">Documented Workflow</div>
          <span className="text-[10px] text-slate-500 font-mono">Zero unauth API risks</span>
        </div>
      </div>

      {/* Workflow Tabs */}
      <div className="p-6 rounded-xl bg-studio-900 border border-studio-800 space-y-4">
        <div className="flex items-center justify-between border-b border-studio-800 pb-3">
          <div className="flex items-center space-x-2">
            <button
              onClick={() => setActiveTab('prompts')}
              className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-all ${
                activeTab === 'prompts'
                  ? 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/30'
                  : 'text-slate-400 hover:text-white'
              }`}
            >
              Quick-Copy Prompt Sheet
            </button>
            <button
              onClick={() => setActiveTab('instructions')}
              className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-all ${
                activeTab === 'instructions'
                  ? 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/30'
                  : 'text-slate-400 hover:text-white'
              }`}
            >
              Google Flow Production Instructions (Markdown)
            </button>
          </div>

          <button
            onClick={handleCopyAll}
            className="text-xs text-cinema-gold hover:text-white flex items-center space-x-1.5 font-mono"
          >
            {copiedAll ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
            <span>{copiedAll ? 'All Prompts Copied!' : 'Copy Entire Sheet'}</span>
          </button>
        </div>

        {activeTab === 'prompts' ? (
          <div className="space-y-4">
            <div className="bg-studio-950 p-4 rounded-xl border border-studio-800 text-xs font-mono text-slate-300 max-h-[500px] overflow-y-auto whitespace-pre-wrap leading-relaxed shadow-inner">
              {pkgInfo?.quick_copy_txt || 'Generating Flow prompt sheet...'}
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            <div className="bg-studio-950 p-6 rounded-xl border border-studio-800 text-xs text-slate-300 max-h-[500px] overflow-y-auto font-mono whitespace-pre-wrap leading-relaxed shadow-inner">
              {pkgInfo?.instructions_markdown || 'Generating production instructions...'}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};
