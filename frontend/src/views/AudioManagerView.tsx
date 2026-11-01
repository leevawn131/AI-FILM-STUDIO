import React, { useState, useEffect } from 'react';
import {
  Project,
  AudioTrack,
  fetchAudioTracks,
  uploadAudio,
  generateBgm,
  generateVoiceover,
  updateAudioTrack,
  deleteAudioTrack,
} from '../api/client';
import {
  Music,
  Mic,
  Plus,
  Play,
  Pause,
  Trash2,
  Volume2,
  Sparkles,
  Sliders,
  CheckCircle2,
  Upload,
  Radio,
  Disc
} from 'lucide-react';

interface AudioManagerViewProps {
  activeProject: Project | null;
  onRefreshProject: () => void;
}

export const AudioManagerView: React.FC<AudioManagerViewProps> = ({
  activeProject,
  onRefreshProject,
}) => {
  const [tracks, setTracks] = useState<AudioTrack[]>([]);
  const [loading, setLoading] = useState(false);
  const [generatingBgm, setGeneratingBgm] = useState(false);
  const [generatingVo, setGeneratingVo] = useState(false);
  const [uploading, setUploading] = useState(false);

  // Audio playback state
  const [playingTrackId, setPlayingTrackId] = useState<string | null>(null);
  const [audioEl, setAudioEl] = useState<HTMLAudioElement | null>(null);

  // Modals
  const [showBgmModal, setShowBgmModal] = useState(false);
  const [bgmTitle, setBgmTitle] = useState('Atmospheric Dark Synthwave');
  const [bgmDuration, setBgmDuration] = useState(25.0);
  const [bgmMood, setBgmMood] = useState('cyberpunk');

  const [showVoModal, setShowVoModal] = useState(false);
  const [voChar, setVoChar] = useState('REN SHINODA');
  const [voText, setVoText] = useState('If this neural sequence fails, the entire spire goes dark.');
  const [voDuration, setVoDuration] = useState(4.0);

  const loadTracks = async () => {
    if (!activeProject) return;
    setLoading(true);
    try {
      const data = await fetchAudioTracks(activeProject.id);
      setTracks(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadTracks();
    return () => {
      if (audioEl) audioEl.pause();
    };
  }, [activeProject?.id]);

  const handlePlayToggle = (track: AudioTrack) => {
    if (playingTrackId === track.id) {
      if (audioEl) audioEl.pause();
      setPlayingTrackId(null);
    } else {
      if (audioEl) audioEl.pause();
      const el = new Audio(track.url);
      el.volume = track.volume_level;
      el.onended = () => setPlayingTrackId(null);
      el.play();
      setAudioEl(el);
      setPlayingTrackId(track.id);
    }
  };

  const handleVolumeChange = async (trackId: string, vol: number) => {
    try {
      await updateAudioTrack(trackId, { volume_level: vol });
      setTracks((prev) =>
        prev.map((t) => (t.id === trackId ? { ...t, volume_level: vol } : t))
      );
      if (audioEl && playingTrackId === trackId) {
        audioEl.volume = vol;
      }
    } catch (err) {
      console.error(err);
    }
  };

  const handleDuckingToggle = async (trackId: string, current: boolean) => {
    try {
      await updateAudioTrack(trackId, { ducking_enabled: !current });
      setTracks((prev) =>
        prev.map((t) => (t.id === trackId ? { ...t, ducking_enabled: !current } : t))
      );
    } catch (err) {
      console.error(err);
    }
  };

  const handleDelete = async (trackId: string) => {
    if (!window.confirm('Delete this audio track?')) return;
    try {
      await deleteAudioTrack(trackId);
      if (playingTrackId === trackId && audioEl) {
        audioEl.pause();
        setPlayingTrackId(null);
      }
      await loadTracks();
    } catch (err) {
      alert('Delete failed: ' + err);
    }
  };

  const handleCreateBgm = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeProject) return;
    setGeneratingBgm(true);
    try {
      await generateBgm(activeProject.id, bgmTitle, bgmDuration, bgmMood);
      setShowBgmModal(false);
      await loadTracks();
      onRefreshProject();
    } catch (err) {
      alert('BGM synthesis error: ' + err);
    } finally {
      setGeneratingBgm(false);
    }
  };

  const handleCreateVo = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeProject || !voText.trim()) return;
    setGeneratingVo(true);
    try {
      await generateVoiceover(activeProject.id, voChar, voText, voDuration);
      setShowVoModal(false);
      await loadTracks();
      onRefreshProject();
    } catch (err) {
      alert('Voiceover error: ' + err);
    } finally {
      setGeneratingVo(false);
    }
  };

  const handleUploadAudio = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file || !activeProject) return;
    setUploading(true);
    try {
      await uploadAudio(activeProject.id, file, 'music', file.name);
      await loadTracks();
      onRefreshProject();
    } catch (err) {
      alert('Upload failed: ' + err);
    } finally {
      setUploading(false);
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
            <h2 className="text-xl font-bold text-white tracking-tight">Audio & Voiceover Studio</h2>
            <span className="text-[10px] bg-purple-500/10 text-purple-400 border border-purple-500/30 px-2 py-0.5 rounded font-mono font-medium">
              Multi-Track Mixer & Ducking
            </span>
          </div>
          <p className="text-xs text-slate-400">
            Synthesize cinematic ambient scores, record voiceover scratch dialogue, and configure automatic sidechain ducking
          </p>
        </div>

        <div className="flex items-center space-x-3">
          <button
            onClick={() => setShowBgmModal(true)}
            className="bg-gradient-to-r from-purple-600 to-indigo-600 hover:opacity-95 text-white px-3.5 py-2 rounded-lg text-xs font-semibold flex items-center space-x-1.5 shadow-md shadow-purple-900/20 transition-all"
          >
            <Sparkles className="w-4 h-4" />
            <span>Generate Ambient Score</span>
          </button>

          <button
            onClick={() => setShowVoModal(true)}
            className="bg-studio-900 hover:bg-studio-800 text-cyan-300 border border-cyan-800/40 px-3.5 py-2 rounded-lg text-xs font-semibold flex items-center space-x-1.5 transition-all shadow"
          >
            <Mic className="w-4 h-4 text-cyan-400" />
            <span>Scratch Voiceover</span>
          </button>

          <label className="bg-cinema-gold hover:bg-cinema-amber text-studio-950 px-4 py-2 rounded-lg text-xs font-bold flex items-center space-x-1.5 cursor-pointer shadow-md shadow-cinema-amber/10 transition-all active:scale-95">
            <Upload className="w-4 h-4 stroke-[2.5]" />
            <span>{uploading ? 'Importing...' : 'Upload Audio'}</span>
            <input
              type="file"
              accept="audio/*"
              className="hidden"
              disabled={uploading}
              onChange={handleUploadAudio}
            />
          </label>
        </div>
      </div>

      {/* Synthesis Modals */}
      {showBgmModal && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-studio-900 border border-studio-700 rounded-xl max-w-md w-full p-6 space-y-4 shadow-2xl">
            <div className="flex items-center space-x-2 text-purple-400">
              <Disc className="w-5 h-5" />
              <h3 className="text-sm font-bold uppercase tracking-wider">Synthesize Cinematic Score</h3>
            </div>
            <p className="text-xs text-slate-400">
              Generates harmonic drone chords with analog flanger and sub-bass textures via FFmpeg lavfi filters.
            </p>

            <form onSubmit={handleCreateBgm} className="space-y-3">
              <div>
                <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Track Title</label>
                <input
                  type="text"
                  required
                  value={bgmTitle}
                  onChange={(e) => setBgmTitle(e.target.value)}
                  className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Duration (s)</label>
                  <input
                    type="number"
                    min={5}
                    max={120}
                    value={bgmDuration}
                    onChange={(e) => setBgmDuration(Number(e.target.value))}
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white font-mono"
                  />
                </div>
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Mood</label>
                  <select
                    value={bgmMood}
                    onChange={(e) => setBgmMood(e.target.value)}
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                  >
                    <option value="cyberpunk">Cyberpunk Minor</option>
                    <option value="space">Deep Space Drone</option>
                    <option value="noir">Noir Mystery</option>
                  </select>
                </div>
              </div>

              <div className="flex justify-end space-x-2 pt-2 border-t border-studio-800">
                <button
                  type="button"
                  onClick={() => setShowBgmModal(false)}
                  className="px-3 py-1.5 rounded text-xs text-slate-400 hover:text-white"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={generatingBgm}
                  className="bg-purple-600 hover:bg-purple-500 text-white px-4 py-1.5 rounded text-xs font-bold"
                >
                  {generatingBgm ? 'Synthesizing...' : 'Synthesize Audio'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {showVoModal && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-studio-900 border border-studio-700 rounded-xl max-w-md w-full p-6 space-y-4 shadow-2xl">
            <div className="flex items-center space-x-2 text-cyan-400">
              <Mic className="w-5 h-5" />
              <h3 className="text-sm font-bold uppercase tracking-wider">Scratch Voiceover Generator</h3>
            </div>
            <p className="text-xs text-slate-400">
              Synthesizes radio-comm filtered dialogue scratch track for pacing and ducking tests.
            </p>

            <form onSubmit={handleCreateVo} className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Speaker</label>
                  <input
                    type="text"
                    required
                    value={voChar}
                    onChange={(e) => setVoChar(e.target.value)}
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white font-bold uppercase"
                  />
                </div>
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Duration (s)</label>
                  <input
                    type="number"
                    step={0.5}
                    value={voDuration}
                    onChange={(e) => setVoDuration(Number(e.target.value))}
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white font-mono"
                  />
                </div>
              </div>

              <div>
                <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Dialogue Text</label>
                <textarea
                  rows={2}
                  required
                  value={voText}
                  onChange={(e) => setVoText(e.target.value)}
                  className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white font-mono"
                />
              </div>

              <div className="flex justify-end space-x-2 pt-2 border-t border-studio-800">
                <button
                  type="button"
                  onClick={() => setShowVoModal(false)}
                  className="px-3 py-1.5 rounded text-xs text-slate-400 hover:text-white"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={generatingVo}
                  className="bg-cyan-500 hover:bg-cyan-400 text-studio-950 px-4 py-1.5 rounded text-xs font-bold"
                >
                  {generatingVo ? 'Generating...' : 'Create Voiceover'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Auto-Ducking Info Banner */}
      <div className="p-4 rounded-xl bg-studio-900 border border-studio-800 flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <div className="w-8 h-8 rounded-lg bg-purple-500/10 text-purple-400 flex items-center justify-center">
            <Radio className="w-4 h-4" />
          </div>
          <div>
            <h4 className="text-xs font-bold text-white uppercase tracking-wider">Sidechain Audio Ducking Active</h4>
            <p className="text-[11px] text-slate-400">
              When dialogue or voiceover occurs, background music automatically ducks to 25% volume (-12dB) to ensure maximum vocal intelligibility.
            </p>
          </div>
        </div>
        <span className="text-xs font-mono font-bold text-emerald-400 bg-studio-950 px-2.5 py-1 rounded border border-studio-800">
          -14 LUFS Target
        </span>
      </div>

      {/* Audio Tracks Mixer List */}
      <div className="space-y-3">
        {tracks.map((track) => {
          const isPlaying = playingTrackId === track.id;
          const isMusic = track.track_type === 'music';

          return (
            <div
              key={track.id}
              className="p-4 rounded-xl bg-studio-900 border border-studio-800 hover:border-studio-700 transition-all flex flex-wrap items-center justify-between gap-4"
            >
              {/* Left: Play button & info */}
              <div className="flex items-center space-x-3.5 min-w-[240px]">
                <button
                  onClick={() => handlePlayToggle(track)}
                  className={`w-9 h-9 rounded-full flex items-center justify-center shadow transition-all ${
                    isPlaying
                      ? 'bg-cinema-gold text-studio-950 scale-105'
                      : 'bg-studio-800 text-slate-300 hover:bg-studio-700 hover:text-white'
                  }`}
                >
                  {isPlaying ? <Pause className="w-4 h-4 fill-current" /> : <Play className="w-4 h-4 fill-current ml-0.5" />}
                </button>

                <div>
                  <div className="flex items-center space-x-2">
                    <span className="text-xs font-bold text-white tracking-wide">{track.title}</span>
                    <span
                      className={`text-[9px] font-mono font-bold px-1.5 py-0.5 rounded uppercase ${
                        isMusic
                          ? 'bg-purple-500/20 text-purple-300 border border-purple-500/30'
                          : 'bg-cyan-500/20 text-cyan-300 border border-cyan-500/30'
                      }`}
                    >
                      {track.track_type}
                    </span>
                  </div>
                  <span className="text-[10px] font-mono text-slate-400">
                    Duration: {track.duration_seconds}s • Stereo 44.1kHz
                  </span>
                </div>
              </div>

              {/* Middle: Volume Slider */}
              <div className="flex items-center space-x-3 min-w-[200px]">
                <Volume2 className="w-4 h-4 text-slate-400 shrink-0" />
                <div className="flex-1 space-y-1">
                  <div className="flex justify-between text-[10px] font-mono text-slate-400">
                    <span>Gain</span>
                    <span>{Math.round(track.volume_level * 100)}%</span>
                  </div>
                  <input
                    type="range"
                    min={0.0}
                    max={2.0}
                    step={0.05}
                    value={track.volume_level}
                    onChange={(e) => handleVolumeChange(track.id, Number(e.target.value))}
                    className="w-full accent-cinema-gold bg-studio-950 h-1.5 rounded-lg cursor-pointer"
                  />
                </div>
              </div>

              {/* Ducking toggle for BGM */}
              {isMusic && (
                <div className="flex items-center space-x-2">
                  <span className="text-[10px] font-mono text-slate-400 uppercase">Auto Duck:</span>
                  <button
                    onClick={() => handleDuckingToggle(track.id, track.ducking_enabled)}
                    className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded border transition-colors ${
                      track.ducking_enabled
                        ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/40'
                        : 'bg-studio-950 text-slate-500 border-studio-800'
                    }`}
                  >
                    {track.ducking_enabled ? 'ENABLED' : 'DISABLED'}
                  </button>
                </div>
              )}

              {/* Right: Delete */}
              <button
                onClick={() => handleDelete(track.id)}
                className="text-slate-500 hover:text-cinema-crimson p-1.5 rounded transition-colors"
              >
                <Trash2 className="w-4 h-4" />
              </button>
            </div>
          );
        })}

        {tracks.length === 0 && !loading && (
          <div className="p-12 text-center text-slate-500 text-xs font-mono space-y-3 bg-studio-900 rounded-xl border border-studio-800">
            <Music className="w-8 h-8 text-slate-600 mx-auto" />
            <p>No audio tracks configured for this film.</p>
            <p className="text-slate-400">
              Click <strong className="text-purple-400">"Generate Ambient Score"</strong> to synthesize local BGM with FFmpeg!
            </p>
          </div>
        )}
      </div>
    </div>
  );
};
