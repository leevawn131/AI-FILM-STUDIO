import React, { useState, useEffect } from 'react';
import {
  Project,
  ExportJob,
  PresetConfig,
  fetchExportPresets,
  fetchExportJobs,
  launchExportRender,
} from '../api/client';
import {
  Download,
  Film,
  Play,
  CheckCircle2,
  Clock,
  Sparkles,
  HardDrive,
  FileCheck,
  Video,
  AlertCircle,
  X
} from 'lucide-react';

interface ExportCenterViewProps {
  activeProject: Project | null;
  onRefreshProject: () => void;
}

export const ExportCenterView: React.FC<ExportCenterViewProps> = ({
  activeProject,
  onRefreshProject,
}) => {
  const [presets, setPresets] = useState<Record<string, PresetConfig>>({});
  const [jobs, setJobs] = useState<ExportJob[]>([]);
  const [selectedPreset, setSelectedPreset] = useState<string>('1080p_cinematic');
  const [subtitles, setSubtitles] = useState(true);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [activePreviewUrl, setActivePreviewUrl] = useState<string | null>(null);

  const loadData = async () => {
    if (!activeProject) return;
    setLoading(true);
    try {
      const [presetsData, jobsData] = await Promise.all([
        fetchExportPresets(),
        fetchExportJobs(activeProject.id),
      ]);
      setPresets(presetsData);
      setJobs(jobsData);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, [activeProject?.id]);

  const handleLaunchExport = async () => {
    if (!activeProject) return;
    setExporting(true);
    try {
      const cfg = presets[selectedPreset] || {
        resolution: '1920x1080',
        fps: 24,
      };
      await launchExportRender(
        activeProject.id,
        selectedPreset,
        cfg.resolution,
        cfg.fps,
        subtitles
      );
      await loadData();
      onRefreshProject();
    } catch (err: any) {
      alert('Export failed: ' + (err.message || err));
    } finally {
      setExporting(false);
    }
  };

  if (!activeProject) {
    return <div className="p-8 text-center text-slate-400">Please select a project first.</div>;
  }

  return (
    <div className="p-8 space-y-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="flex items-center space-x-2">
            <h2 className="text-xl font-bold text-white tracking-tight">Master Export Center</h2>
            <span className="text-[10px] bg-cinema-gold/15 text-cinema-gold border border-cinema-gold/30 px-2 py-0.5 rounded font-mono font-medium">
              FFmpeg Hardware Acceleration Ready
            </span>
          </div>
          <p className="text-xs text-slate-400">
            Render theatrical masters, web previews, and social vertical cuts with integrated multi-track audio and burn-in subtitles
          </p>
        </div>

        <button
          onClick={handleLaunchExport}
          disabled={exporting}
          className="bg-gradient-to-r from-cinema-amber to-cinema-gold hover:opacity-95 text-studio-950 font-bold px-5 py-2.5 rounded-xl text-xs flex items-center space-x-2 shadow-lg shadow-cinema-amber/15 transition-all active:scale-95 disabled:opacity-50"
        >
          <Film className="w-4 h-4 stroke-[2.5]" />
          <span>{exporting ? 'Rendering Master MP4...' : 'Render Master Film'}</span>
        </button>
      </div>

      {/* Video Preview Modal */}
      {activePreviewUrl && (
        <div className="fixed inset-0 z-50 bg-black/90 backdrop-blur-md flex items-center justify-center p-4">
          <div className="relative max-w-4xl w-full bg-studio-900 border border-studio-700 rounded-2xl overflow-hidden shadow-2xl">
            <div className="p-4 border-b border-studio-800 flex items-center justify-between">
              <span className="text-xs font-mono font-bold text-white">Master Film Player</span>
              <button
                onClick={() => setActivePreviewUrl(null)}
                className="text-slate-400 hover:text-white transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
            <div className="aspect-video bg-black flex items-center justify-center">
              <video
                src={activePreviewUrl}
                controls
                autoPlay
                className="w-full h-full object-contain"
              />
            </div>
          </div>
        </div>
      )}

      {/* Render Preset Selection Cards */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-bold uppercase tracking-wider text-white">Select Export Preset</h3>
          <label className="flex items-center space-x-2 text-xs text-slate-300 font-mono cursor-pointer">
            <input
              type="checkbox"
              checked={subtitles}
              onChange={(e) => setSubtitles(e.target.checked)}
              className="accent-cinema-gold rounded"
            />
            <span>Burn-in Screenplay Subtitles</span>
          </label>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          {Object.entries(presets).map(([key, cfg]) => {
            const isSelected = selectedPreset === key;
            return (
              <div
                key={key}
                onClick={() => setSelectedPreset(key)}
                className={`p-4 rounded-xl border cursor-pointer transition-all flex flex-col justify-between ${
                  isSelected
                    ? 'bg-studio-900 border-cinema-gold shadow-lg shadow-cinema-amber/10 ring-1 ring-cinema-gold/30'
                    : 'bg-studio-900/60 border-studio-800 hover:border-studio-700'
                }`}
              >
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] font-mono font-bold px-2 py-0.5 rounded bg-studio-950 text-cyan-400 border border-cyan-800/30">
                      {cfg.resolution}
                    </span>
                    <span className="text-[10px] font-mono text-slate-400">{cfg.fps} FPS</span>
                  </div>
                  <h4 className="text-sm font-bold text-white">{cfg.name}</h4>
                  <p className="text-[11px] text-slate-400 leading-snug">{cfg.description}</p>
                </div>

                <div className="pt-3 border-t border-studio-800/80 mt-3 flex items-center justify-between text-[10px] font-mono">
                  <span className="text-slate-500">{cfg.aspect_ratio} Aspect</span>
                  {isSelected && <span className="text-cinema-gold font-bold">SELECTED</span>}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Export Jobs History */}
      <div className="p-6 rounded-xl bg-studio-900 border border-studio-800 space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-bold uppercase tracking-wider text-white">Render Jobs & Master Downloads</h3>
          <span className="text-[10px] font-mono text-slate-400">{jobs.length} export jobs recorded</span>
        </div>

        <div className="space-y-3">
          {jobs.map((job) => {
            const isDone = job.status === 'completed';
            const sizeMb = job.file_size_bytes ? (job.file_size_bytes / (1024 * 1024)).toFixed(1) : '0';

            return (
              <div
                key={job.id}
                className="p-4 rounded-xl bg-studio-950 border border-studio-800/80 flex flex-wrap items-center justify-between gap-4"
              >
                <div className="flex items-center space-x-3.5">
                  <div className="w-9 h-9 rounded-lg bg-cinema-amber/10 text-cinema-gold flex items-center justify-center shrink-0">
                    <Film className="w-4 h-4" />
                  </div>
                  <div>
                    <div className="flex items-center space-x-2">
                      <span className="text-xs font-bold text-white">{job.preset_name.toUpperCase()}</span>
                      <span className="text-[10px] font-mono bg-studio-900 text-cyan-400 px-2 py-0.5 rounded border border-cyan-800/30">
                        {job.resolution}
                      </span>
                      <span className="text-[10px] font-mono text-slate-400">{job.fps} FPS</span>
                    </div>
                    <span className="text-[10px] font-mono text-slate-400">
                      Rendered on {new Date(job.created_at).toLocaleString()} • {sizeMb} MB
                    </span>
                  </div>
                </div>

                <div className="flex items-center space-x-3">
                  <span
                    className={`text-[10px] font-mono font-bold px-2.5 py-1 rounded border ${
                      isDone
                        ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                        : 'bg-cinema-amber/10 text-cinema-gold border-cinema-amber/30'
                    }`}
                  >
                    {isDone ? 'COMPLETED (100%)' : `RENDERING (${job.progress_pct}%)`}
                  </span>

                  {isDone && job.download_url && (
                    <>
                      <button
                        onClick={() => setActivePreviewUrl(job.download_url!)}
                        className="bg-studio-850 hover:bg-studio-800 text-white px-3 py-1.5 rounded-lg text-xs font-semibold flex items-center space-x-1.5 transition-colors border border-studio-700"
                      >
                        <Play className="w-3.5 h-3.5 fill-current" />
                        <span>Watch</span>
                      </button>

                      <a
                        href={job.download_url}
                        download
                        className="bg-cinema-gold hover:bg-cinema-amber text-studio-950 px-3.5 py-1.5 rounded-lg text-xs font-bold flex items-center space-x-1.5 shadow transition-all"
                      >
                        <Download className="w-3.5 h-3.5 stroke-[2.5]" />
                        <span>Download MP4</span>
                      </a>
                    </>
                  )}
                </div>
              </div>
            );
          })}

          {jobs.length === 0 && !loading && (
            <div className="py-8 text-center text-slate-500 text-xs font-mono">
              No export jobs yet. Select a preset above and click "Render Master Film" to export your first master cut!
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
