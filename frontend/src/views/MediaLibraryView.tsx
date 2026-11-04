import React, { useState, useEffect } from 'react';
import {
  Project,
  MediaAsset,
  WatcherStatus,
  fetchMediaAssets,
  uploadMedia,
  scanWatchedFolder,
  fetchWatcherStatus,
  deleteMediaAsset,
  generateSyntheticTestClip,
} from '../api/client';
import {
  FolderInput,
  Upload,
  RefreshCw,
  Video,
  Play,
  Trash2,
  CheckCircle2,
  Clock,
  Sparkles,
  HardDrive,
  Eye,
  FileCheck,
  X
} from 'lucide-react';

interface MediaLibraryViewProps {
  activeProject: Project | null;
  onRefreshProject: () => void;
}

export const MediaLibraryView: React.FC<MediaLibraryViewProps> = ({
  activeProject,
  onRefreshProject,
}) => {
  const [assets, setAssets] = useState<MediaAsset[]>([]);
  const [watcherStatus, setWatcherStatus] = useState<WatcherStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [generatingTest, setGeneratingTest] = useState(false);

  // Video preview modal
  const [activeVideoUrl, setActiveVideoUrl] = useState<string | null>(null);

  // Scan result summary
  const [scanResult, setScanResult] = useState<{
    status: string;
    ingested_count: number;
    duplicates_count: number;
    skipped_in_flight_count: number;
    errors_count: number;
    files?: any[];
  } | null>(null);

  const loadData = async () => {
    if (!activeProject) return;
    setLoading(true);
    try {
      const [mediaList, wStatus] = await Promise.all([
        fetchMediaAssets(activeProject.id),
        fetchWatcherStatus(),
      ]);
      setAssets(mediaList);
      setWatcherStatus(wStatus);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, [activeProject?.id]);

  const handleScanWatcher = async () => {
    if (!activeProject) return;
    setScanning(true);
    try {
      const res = await scanWatchedFolder(activeProject.id);
      setScanResult(res);
      await loadData();
      onRefreshProject();
    } catch (err) {
      alert('Scan failed: ' + err);
    } finally {
      setScanning(false);
    }
  };

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file || !activeProject) return;
    setUploading(true);
    try {
      await uploadMedia(activeProject.id, file);
      await loadData();
      onRefreshProject();
    } catch (err) {
      alert('Upload failed: ' + err);
    } finally {
      setUploading(false);
    }
  };

  const handleGenerateSyntheticTestClip = async () => {
    if (!activeProject) return;
    setGeneratingTest(true);
    try {
      await generateSyntheticTestClip(activeProject.id, 'SC01_SH01');
      await loadData();
      onRefreshProject();
    } catch (err) {
      alert('Test clip creation failed: ' + err);
    } finally {
      setGeneratingTest(false);
    }
  };

  const handleDeleteAsset = async (assetId: string) => {
    if (!window.confirm('Delete this media asset from project library?')) return;
    try {
      await deleteMediaAsset(assetId);
      await loadData();
      onRefreshProject();
    } catch (err) {
      alert('Delete failed: ' + err);
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
            <h2 className="text-xl font-bold text-white tracking-tight">Media Library & Import Manager</h2>
            <span className="text-[10px] bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 px-2 py-0.5 rounded font-mono font-medium">
              FFprobe Validated
            </span>
          </div>
          <p className="text-xs text-slate-400">
            Monitor watched download directory, validate video codecs, and automatically match clips to storyboard shots
          </p>
        </div>

        <div className="flex items-center space-x-3">
          <button
            onClick={handleGenerateSyntheticTestClip}
            disabled={generatingTest}
            className="bg-studio-900 hover:bg-studio-800 text-cyan-400 border border-cyan-800/40 px-3.5 py-2 rounded-lg text-xs font-semibold flex items-center space-x-1.5 transition-all shadow"
          >
            <Sparkles className="w-4 h-4 text-cyan-400" />
            <span>{generatingTest ? 'Synthesizing...' : 'Generate Local Test Clip'}</span>
          </button>

          <label className="bg-cinema-gold hover:bg-cinema-amber text-studio-950 px-4 py-2 rounded-lg text-xs font-bold flex items-center space-x-1.5 cursor-pointer shadow-md shadow-cinema-amber/10 transition-all active:scale-95">
            <Upload className="w-4 h-4 stroke-[2.5]" />
            <span>{uploading ? 'Importing...' : 'Upload Video File'}</span>
            <input
              type="file"
              accept="video/*"
              className="hidden"
              disabled={uploading}
              onChange={handleFileUpload}
            />
          </label>
        </div>
      </div>

      {/* Watched Folder Monitor Card */}
      <div className="p-5 rounded-xl bg-studio-900 border border-studio-800 flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-start space-x-3.5">
          <div className="w-10 h-10 rounded-lg bg-emerald-500/10 text-emerald-400 flex items-center justify-center shrink-0">
            <HardDrive className="w-5 h-5" />
          </div>
          <div>
            <div className="flex items-center space-x-2">
              <span className="text-xs font-bold text-white uppercase tracking-wider">Watched Ingestion Directory</span>
              <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
              <span className="text-[10px] text-emerald-400 font-mono">Active</span>
            </div>
            <p className="text-xs text-slate-300 font-mono mt-0.5 truncate max-w-xl">
              {watcherStatus?.watch_directory_path || 'Loading watch path...'}
            </p>
            <span className="text-[11px] text-slate-500 font-mono">
              Drop downloaded Google Flow MP4 clips here. Studio auto-matches filenames containing shot codes (e.g. SC01_SH01.mp4).
            </span>
          </div>
        </div>

        <button
          onClick={handleScanWatcher}
          disabled={scanning}
          className="bg-studio-800 hover:bg-studio-700 text-white border border-studio-700 px-4 py-2 rounded-lg text-xs font-semibold flex items-center space-x-2 transition-all"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${scanning ? 'animate-spin' : ''}`} />
          <span>{scanning ? 'Scanning...' : 'Scan Folder Now'}</span>
        </button>
      </div>

      {/* Watched Ingestion Scan Results Notification */}
      {scanResult && (
        <div className="p-4 rounded-xl bg-studio-900 border border-cinema-gold/40 shadow-lg space-y-2">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-2">
              <span className="font-bold text-xs text-white uppercase font-mono">Scan Results Summary</span>
              <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-cinema-gold/20 text-cinema-gold font-bold">
                {scanResult.ingested_count} Ingested
              </span>
            </div>
            <button
              onClick={() => setScanResult(null)}
              className="text-slate-400 hover:text-white text-xs"
            >
              ✕ Dismiss
            </button>
          </div>
          <div className="flex flex-wrap gap-4 text-xs font-mono text-slate-300">
            <span>✅ <strong className="text-emerald-400">{scanResult.ingested_count}</strong> new footage files imported</span>
            <span>🔁 <strong className="text-amber-400">{scanResult.duplicates_count}</strong> duplicates archived</span>
            <span>⏳ <strong className="text-blue-400">{scanResult.skipped_in_flight_count}</strong> in-flight downloads skipped</span>
            {scanResult.errors_count > 0 && (
              <span>❌ <strong className="text-rose-400">{scanResult.errors_count}</strong> errors encountered</span>
            )}
          </div>
        </div>
      )}

      {/* Video Preview Modal */}
      {activeVideoUrl && (
        <div className="fixed inset-0 z-50 bg-black/90 backdrop-blur-md flex items-center justify-center p-4">
          <div className="relative max-w-4xl w-full bg-studio-900 border border-studio-700 rounded-2xl overflow-hidden shadow-2xl">
            <div className="p-4 border-b border-studio-800 flex items-center justify-between">
              <span className="text-xs font-mono font-bold text-white">Video Asset Player</span>
              <button
                onClick={() => setActiveVideoUrl(null)}
                className="text-slate-400 hover:text-white transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
            <div className="aspect-video bg-black flex items-center justify-center">
              <video
                src={activeVideoUrl}
                controls
                autoPlay
                className="w-full h-full object-contain"
              />
            </div>
          </div>
        </div>
      )}

      {/* Media Ingestion Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {assets.map((asset) => {
          const thumbUrl = asset.thumbnail_path ? `/media/${asset.thumbnail_path}` : null;
          const videoUrl = `/media/${asset.file_path}`;

          return (
            <div
              key={asset.id}
              className="rounded-xl bg-studio-900 border border-studio-800 overflow-hidden shadow-lg flex flex-col justify-between hover:border-studio-700 transition-all"
            >
              {/* Media Thumbnail & Player Trigger */}
              <div
                onClick={() => setActiveVideoUrl(videoUrl)}
                className="relative aspect-video bg-studio-950 cursor-pointer group flex items-center justify-center overflow-hidden"
              >
                {thumbUrl ? (
                  <img src={thumbUrl} alt={asset.filename} className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300" />
                ) : (
                  <Video className="w-10 h-10 text-slate-700" />
                )}
                <div className="absolute inset-0 bg-black/40 group-hover:bg-black/20 flex items-center justify-center transition-colors">
                  <div className="w-12 h-12 rounded-full bg-cinema-gold/90 text-studio-950 flex items-center justify-center shadow-lg group-hover:scale-110 transition-transform">
                    <Play className="w-5 h-5 fill-current ml-0.5" />
                  </div>
                </div>

                {/* Duration badge */}
                <div className="absolute bottom-2 right-2 px-2 py-0.5 rounded bg-black/80 font-mono text-[10px] text-white">
                  {asset.duration_seconds}s
                </div>
              </div>

              {/* Asset Info & Technical Metadata */}
              <div className="p-4 space-y-3">
                <div className="flex items-start justify-between">
                  <div>
                    <div className="flex items-center space-x-1.5">
                      {asset.version && (
                        <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-black/60 border border-cinema-gold/40 text-cinema-gold font-bold">
                          v{asset.version}
                        </span>
                      )}
                      <h3 className="text-xs font-bold text-white font-mono truncate max-w-[170px]" title={asset.filename}>
                        {asset.filename}
                      </h3>
                    </div>
                    <div className="flex items-center space-x-1.5 text-[10px] text-slate-400 font-mono mt-0.5">
                      <span>{asset.width}x{asset.height}</span>
                      <span>•</span>
                      <span>{asset.fps} FPS</span>
                      <span>•</span>
                      <span>{asset.codec.toUpperCase()}</span>
                    </div>
                  </div>

                  <button
                    onClick={() => handleDeleteAsset(asset.id)}
                    className="text-slate-500 hover:text-cinema-crimson p-1 transition-colors"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>

                {/* Auto-match status badge */}
                <div className="p-2.5 rounded-lg bg-studio-950 border border-studio-800/80 flex items-center justify-between text-xs">
                  <div className="flex items-center space-x-2">
                    <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0" />
                    <div>
                      <span className="text-[10px] uppercase font-mono text-slate-400 block">Storyboard Slot</span>
                      <span className="font-mono font-bold text-emerald-400">
                        {asset.auto_matched_shot_id ? 'Auto-Matched & Linked' : 'Unassigned Asset'}
                      </span>
                    </div>
                  </div>

                  <span className="text-[10px] font-mono text-slate-500">
                    {(asset.file_size_bytes / (1024 * 1024)).toFixed(1)} MB
                  </span>
                </div>
              </div>

              {/* Footer */}
              <div className="px-4 py-2.5 bg-studio-950/60 border-t border-studio-800 flex items-center justify-between text-xs text-slate-400">
                <span className="flex items-center space-x-1 text-[10px] font-mono">
                  <FileCheck className="w-3.5 h-3.5 text-emerald-400" />
                  <span>FFprobe Validated</span>
                </span>

                <button
                  onClick={() => setActiveVideoUrl(videoUrl)}
                  className="text-cinema-gold hover:text-white font-semibold text-[11px] transition-colors"
                >
                  Play Video →
                </button>
              </div>
            </div>
          );
        })}

        {assets.length === 0 && !loading && (
          <div className="col-span-3 p-12 text-center text-slate-500 text-xs font-mono space-y-3">
            <FolderInput className="w-8 h-8 text-slate-600 mx-auto" />
            <p>No video clips in media library yet.</p>
            <p className="text-slate-400">
              Click <strong className="text-cyan-400">"Generate Local Test Clip"</strong> or drop videos into the watched folder.
            </p>
          </div>
        )}
      </div>
    </div>
  );
};
