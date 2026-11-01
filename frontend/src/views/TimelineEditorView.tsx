import React, { useState, useEffect, useRef } from 'react';
import {
  Project,
  TimelineData,
  TimelineItem,
  fetchTimeline,
  runAiAssemblyCut,
  renderTimelinePreview,
  RenderResult,
} from '../api/client';
import {
  Scissors,
  Play,
  Pause,
  RotateCcw,
  Sparkles,
  Layers,
  Film,
  Download,
  Clock,
  Video,
  CheckCircle2,
  AlertCircle,
  Eye,
  Sliders
} from 'lucide-react';

interface TimelineEditorViewProps {
  activeProject: Project | null;
  onRefreshProject: () => void;
}

export const TimelineEditorView: React.FC<TimelineEditorViewProps> = ({
  activeProject,
  onRefreshProject,
}) => {
  const [timeline, setTimeline] = useState<TimelineData | null>(null);
  const [loading, setLoading] = useState(false);
  const [rendering, setRendering] = useState(false);
  const [assembling, setAssembling] = useState(false);
  const [selectedItem, setSelectedItem] = useState<TimelineItem | null>(null);

  // Render settings
  const [resolution, setResolution] = useState('1280x720');
  const [fps, setFps] = useState(24);
  const [burnSubtitles, setBurnSubtitles] = useState(true);
  const [lastRender, setLastRender] = useState<RenderResult | null>(null);

  // Player state
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const videoRef = useRef<HTMLVideoElement>(null);

  const loadTimeline = async () => {
    if (!activeProject) return;
    setLoading(true);
    try {
      const data = await fetchTimeline(activeProject.id);
      setTimeline(data);
      if (data.timeline_items.length > 0 && !selectedItem) {
        setSelectedItem(data.timeline_items[0]);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadTimeline();
  }, [activeProject?.id]);

  const handleAssemblyCut = async () => {
    if (!activeProject) return;
    setAssembling(true);
    try {
      const res = await runAiAssemblyCut(activeProject.id);
      alert(`Film Editor Agent: Assembly cut generated with ${res.clip_count} clips!`);
      await loadTimeline();
    } catch (err) {
      alert('Assembly cut failed: ' + err);
    } finally {
      setAssembling(false);
    }
  };

  const handleRenderPreview = async () => {
    if (!activeProject) return;
    setRendering(true);
    try {
      const res = await renderTimelinePreview(activeProject.id, resolution, fps, burnSubtitles);
      setLastRender(res);
      onRefreshProject();
      if (videoRef.current) {
        videoRef.current.src = res.video_url;
        videoRef.current.play();
        setIsPlaying(true);
      }
    } catch (err: any) {
      alert('Render error: ' + (err.message || err));
    } finally {
      setRendering(false);
    }
  };

  const togglePlay = () => {
    if (!videoRef.current) return;
    if (isPlaying) {
      videoRef.current.pause();
      setIsPlaying(false);
    } else {
      videoRef.current.play();
      setIsPlaying(true);
    }
  };

  const handleTimeUpdate = () => {
    if (videoRef.current) {
      setCurrentTime(videoRef.current.currentTime);
    }
  };

  const handleRewind = () => {
    if (videoRef.current) {
      videoRef.current.currentTime = 0;
      setCurrentTime(0);
    }
  };

  if (!activeProject) {
    return <div className="p-8 text-center text-slate-400">Please select a project first.</div>;
  }

  const activeVideoSrc = lastRender?.video_url || selectedItem?.media_url || null;
  const totalDuration = timeline?.total_duration_seconds || 15;

  return (
    <div className="flex flex-col h-full overflow-hidden bg-studio-950">
      {/* Top Controls Bar */}
      <div className="h-14 bg-studio-900 border-b border-studio-800 px-6 flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <div className="flex items-center space-x-2">
            <Scissors className="w-4 h-4 text-cinema-gold" />
            <h2 className="text-sm font-bold text-white uppercase tracking-wider">FFmpeg Timeline Editor</h2>
          </div>
          <span className="text-[10px] font-mono bg-studio-950 text-cyan-400 border border-cyan-800/30 px-2 py-0.5 rounded">
            {activeProject.aspect_ratio} Anamorphic
          </span>
          <span className="text-[10px] font-mono text-slate-400">
            Total Cut: {totalDuration}s ({timeline?.total_shots_count || 0} clips)
          </span>
        </div>

        {/* Render Preset & Actions */}
        <div className="flex items-center space-x-3 text-xs">
          <div className="flex items-center space-x-1.5 font-mono text-slate-400">
            <span>Res:</span>
            <select
              value={resolution}
              onChange={(e) => setResolution(e.target.value)}
              className="bg-studio-950 border border-studio-700 rounded px-2 py-1 text-white text-[11px]"
            >
              <option value="1280x720">1280x720 (Fast 720p)</option>
              <option value="1920x1080">1920x1080 (1080p Cinema)</option>
              <option value="3840x2160">3840x2160 (4K Master)</option>
              <option value="1080x1920">1080x1920 (9:16 Vertical)</option>
            </select>
          </div>

          <button
            onClick={handleAssemblyCut}
            disabled={assembling}
            className="bg-studio-800 hover:bg-studio-700 text-purple-300 border border-purple-800/40 px-3 py-1.5 rounded-lg text-xs font-semibold flex items-center space-x-1.5 transition-all"
          >
            <Sparkles className="w-3.5 h-3.5 text-purple-400" />
            <span>{assembling ? 'Aligning...' : 'AI Assembly Cut'}</span>
          </button>

          <button
            onClick={handleRenderPreview}
            disabled={rendering}
            className="bg-gradient-to-r from-cinema-amber to-cinema-gold hover:opacity-95 text-studio-950 font-bold px-4 py-1.5 rounded-lg text-xs flex items-center space-x-1.5 shadow-md shadow-cinema-amber/15 transition-all active:scale-95 disabled:opacity-50"
          >
            <Film className="w-3.5 h-3.5 stroke-[2.5]" />
            <span>{rendering ? 'Rendering with FFmpeg...' : 'Render Timeline MP4'}</span>
          </button>
        </div>
      </div>

      {/* Main Workspace: Player & Inspector */}
      <div className="flex-1 flex overflow-hidden">
        {/* Video Player Centerpiece */}
        <div className="flex-1 flex flex-col items-center justify-center p-6 bg-black relative">
          <div className="relative w-full max-w-3xl aspect-video bg-studio-950 border border-studio-800 rounded-xl overflow-hidden shadow-2xl flex items-center justify-center">
            {activeVideoSrc ? (
              <video
                ref={videoRef}
                src={activeVideoSrc}
                onTimeUpdate={handleTimeUpdate}
                onEnded={() => setIsPlaying(false)}
                className="w-full h-full object-contain"
              />
            ) : (
              <div className="text-center space-y-2 p-6 text-slate-500 font-mono text-xs">
                <Video className="w-12 h-12 text-slate-700 mx-auto" />
                <p>No rendered timeline cut or selected video asset.</p>
                <p className="text-slate-400">
                  Click <strong className="text-cinema-gold">"Render Timeline MP4"</strong> to synthesize the full sequence with FFmpeg!
                </p>
              </div>
            )}

            {/* Top overlay indicator */}
            {lastRender && (
              <div className="absolute top-3 left-3 bg-black/70 px-2.5 py-1 rounded border border-studio-700 font-mono text-[10px] text-emerald-400 flex items-center space-x-1.5">
                <CheckCircle2 className="w-3.5 h-3.5" />
                <span>Rendered Master: {lastRender.resolution} • {lastRender.duration_seconds}s</span>
              </div>
            )}
          </div>

          {/* Player Transport Controls */}
          <div className="mt-4 flex items-center space-x-4 bg-studio-900 border border-studio-800 px-5 py-2 rounded-xl shadow-lg">
            <button
              onClick={handleRewind}
              className="text-slate-400 hover:text-white transition-colors"
              title="Rewind to start"
            >
              <RotateCcw className="w-4 h-4" />
            </button>

            <button
              onClick={togglePlay}
              className="w-8 h-8 rounded-full bg-cinema-gold text-studio-950 flex items-center justify-center shadow transition-transform active:scale-95"
            >
              {isPlaying ? <Pause className="w-4 h-4 fill-current" /> : <Play className="w-4 h-4 fill-current ml-0.5" />}
            </button>

            <div className="font-mono text-xs text-white">
              <span>{currentTime.toFixed(1)}s</span>
              <span className="text-slate-500 mx-1">/</span>
              <span className="text-slate-400">{totalDuration.toFixed(1)}s</span>
            </div>

            {lastRender && (
              <a
                href={lastRender.video_url}
                download
                className="text-cinema-gold hover:underline text-xs font-mono flex items-center space-x-1 ml-4"
              >
                <Download className="w-3.5 h-3.5" />
                <span>Download MP4</span>
              </a>
            )}
          </div>
        </div>

        {/* Clip Inspector Right Drawer */}
        <div className="w-80 bg-studio-900 border-l border-studio-800 p-4 space-y-4 overflow-y-auto">
          <div className="flex items-center space-x-2 text-cinema-gold border-b border-studio-800 pb-2">
            <Sliders className="w-4 h-4" />
            <h3 className="text-xs font-bold uppercase tracking-wider">Clip Inspector</h3>
          </div>

          {selectedItem ? (
            <div className="space-y-3 text-xs">
              <div>
                <span className="text-[10px] font-mono uppercase text-slate-400 block mb-0.5">Shot Code</span>
                <span className="text-sm font-bold font-mono text-cinema-gold">{selectedItem.shot_code}</span>
              </div>

              <div className="grid grid-cols-2 gap-2 text-[11px] font-mono">
                <div className="bg-studio-950 p-2 rounded border border-studio-800">
                  <span className="text-slate-500 block text-[9px]">Start</span>
                  <span className="text-white">{selectedItem.start_time_seconds}s</span>
                </div>
                <div className="bg-studio-950 p-2 rounded border border-studio-800">
                  <span className="text-slate-500 block text-[9px]">Duration</span>
                  <span className="text-white">{selectedItem.duration_seconds}s</span>
                </div>
              </div>

              <div className="space-y-1">
                <span className="text-[10px] font-mono uppercase text-slate-400">Camera Direction</span>
                <div className="bg-studio-950 p-2.5 rounded-lg border border-studio-800 space-y-1 font-mono text-[11px]">
                  <div className="text-purple-300">{selectedItem.camera_angle}</div>
                  <div className="text-cyan-300">{selectedItem.camera_movement}</div>
                </div>
              </div>

              <div className="space-y-1">
                <span className="text-[10px] font-mono uppercase text-slate-400">Visual Action</span>
                <p className="bg-studio-950 p-2.5 rounded-lg border border-studio-800 text-slate-200 text-xs leading-relaxed">
                  {selectedItem.visual_description}
                </p>
              </div>

              <div className="space-y-1">
                <span className="text-[10px] font-mono uppercase text-slate-400">Footage Asset Link</span>
                <div className="p-2 rounded bg-studio-950 border border-studio-800 flex items-center justify-between font-mono text-[11px]">
                  <span className={selectedItem.media_url ? 'text-emerald-400' : 'text-cinema-amber'}>
                    {selectedItem.media_url ? 'Linked Video Ready' : 'Synthetic Placeholder'}
                  </span>
                  {selectedItem.media_url && (
                    <button
                      onClick={() => {
                        if (videoRef.current) {
                          videoRef.current.src = selectedItem.media_url!;
                          videoRef.current.play();
                          setIsPlaying(true);
                        }
                      }}
                      className="text-cyan-400 hover:underline text-[10px]"
                    >
                      Preview Clip
                    </button>
                  )}
                </div>
              </div>
            </div>
          ) : (
            <div className="text-slate-500 text-xs font-mono">Click a clip on the timeline below to inspect.</div>
          )}
        </div>
      </div>

      {/* Multi-Track NLE Timeline View */}
      <div className="h-60 bg-studio-900 border-t border-studio-800 flex flex-col overflow-hidden">
        {/* Timeline Header & Timecode Ruler */}
        <div className="h-7 bg-studio-950 border-b border-studio-800 px-4 flex items-center text-[10px] font-mono text-slate-500 space-x-8 overflow-hidden select-none">
          <div className="w-16 text-slate-400 font-bold uppercase">TRACKS</div>
          <div className="flex-1 flex justify-between pr-8">
            <span>00:00:00</span>
            <span>00:00:05</span>
            <span>00:00:10</span>
            <span>00:00:15</span>
            <span>00:00:20</span>
            <span>00:00:25</span>
            <span>00:00:30+</span>
          </div>
        </div>

        {/* Tracks Container */}
        <div className="flex-1 overflow-x-auto p-3 space-y-2 select-none">
          {/* Video Track V1 */}
          <div className="flex items-center space-x-2">
            <div className="w-16 text-[10px] font-mono font-bold text-cinema-gold px-1.5 py-0.5 rounded bg-studio-950 border border-cinema-gold/20 shrink-0">
              V1 VIDEO
            </div>
            <div className="flex-1 flex space-x-1.5 min-w-[700px]">
              {timeline?.timeline_items.map((item) => {
                const isSelected = selectedItem?.shot_id === item.shot_id;
                const isImported = Boolean(item.media_url);

                return (
                  <div
                    key={item.shot_id}
                    onClick={() => setSelectedItem(item)}
                    style={{ flexGrow: item.duration_seconds }}
                    className={`h-16 rounded-lg border p-2 cursor-pointer transition-all flex flex-col justify-between overflow-hidden ${
                      isSelected
                        ? 'bg-studio-800 border-cinema-gold shadow-lg shadow-cinema-amber/10'
                        : isImported
                        ? 'bg-studio-850/80 border-emerald-500/40 hover:border-emerald-500'
                        : 'bg-studio-950/70 border-studio-800 hover:border-studio-700'
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="text-[10px] font-mono font-bold text-white truncate">
                        {item.shot_code}
                      </span>
                      <span className="text-[9px] font-mono text-slate-400">{item.duration_seconds}s</span>
                    </div>
                    <span className="text-[9px] font-mono text-slate-400 truncate">
                      {item.camera_angle}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Dialogue / Subtitle Track S1 */}
          <div className="flex items-center space-x-2">
            <div className="w-16 text-[10px] font-mono font-bold text-cyan-400 px-1.5 py-0.5 rounded bg-studio-950 border border-cyan-800/20 shrink-0">
              S1 SUBS
            </div>
            <div className="flex-1 flex space-x-1.5 min-w-[700px]">
              {timeline?.timeline_items.map((item) => (
                <div
                  key={`sub-${item.shot_id}`}
                  style={{ flexGrow: item.duration_seconds }}
                  className="h-7 rounded bg-studio-950/80 border border-studio-800 px-2 flex items-center text-[9px] font-mono text-slate-300 truncate"
                >
                  Captions: {item.shot_code}
                </div>
              ))}
            </div>
          </div>

          {/* Audio Track A1 */}
          <div className="flex items-center space-x-2">
            <div className="w-16 text-[10px] font-mono font-bold text-purple-400 px-1.5 py-0.5 rounded bg-studio-950 border border-purple-800/20 shrink-0">
              A1 BGM
            </div>
            <div className="flex-1 min-w-[700px] h-7 rounded bg-studio-950 border border-purple-900/30 px-3 flex items-center justify-between text-[9px] font-mono text-purple-300">
              <span>♪ Master Film Score (Stereo 44.1kHz • Auto Ducking Active)</span>
              <span>-14 LUFS</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
