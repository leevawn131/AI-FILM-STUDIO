import React, { useState, useEffect } from 'react';
import {
  Project,
  AudioTrack,
  fetchAudioTracks,
  uploadAudio,
  generateGoogleTTSDialogue,
  generateGoogleLyriaMusic,
  fetchTTSVoices,
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
  Disc,
  Headphones,
  SlidersHorizontal
} from 'lucide-react';

interface AudioManagerViewProps {
  activeProject: Project | null;
  onRefreshProject: () => void;
}

const PREBUILT_VOICES = [
  { id: 'Puck', label: 'Puck', desc: 'Energetic, articulate, youthful' },
  { id: 'Charon', label: 'Charon', desc: 'Deep, resonant, authoritative' },
  { id: 'Kore', label: 'Kore', desc: 'Warm, expressive, melodic' },
  { id: 'Fenrir', label: 'Fenrir', desc: 'Gritty, intense, dramatic' },
  { id: 'Aoede', label: 'Aoede', desc: 'Sophisticated, lyrical, calm' },
];

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

  // Lyria Music Modal State
  const [showBgmModal, setShowBgmModal] = useState(false);
  const [bgmTitle, setBgmTitle] = useState('Atmospheric Dark Synthwave');
  const [bgmDuration, setBgmDuration] = useState(30);
  const [bgmMood, setBgmMood] = useState('atmospheric cyberpunk');
  const [bgmGenre, setBgmGenre] = useState('cinematic synthwave');
  const [bgmBpm, setBgmBpm] = useState(110);

  // Gemini TTS Dialogue Modal State
  const [showVoModal, setShowVoModal] = useState(false);
  const [voChar, setVoChar] = useState('REN SHINODA');
  const [voText, setVoText] = useState('If this neural sequence fails, the entire spire goes dark.');
  const [voVoice, setVoVoice] = useState('Puck');
  const [voEmotion, setVoEmotion] = useState('urgent');
  const [voPace, setVoPace] = useState(1.0);

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

  const handleCreateLyriaMusic = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeProject) return;
    setGeneratingBgm(true);
    try {
      await generateGoogleLyriaMusic(activeProject.id, {
        prompt: bgmTitle,
        mood: bgmMood,
        genre: bgmGenre,
        bpm: bgmBpm,
        duration_seconds: bgmDuration,
        instrumental: true
      });
      setShowBgmModal(false);
      await loadTracks();
      onRefreshProject();
    } catch (err: any) {
      alert('Lyria score generation error: ' + (err.message || err));
    } finally {
      setGeneratingBgm(false);
    }
  };

  const handleCreateTTSDialogue = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeProject || !voText.trim()) return;
    setGeneratingVo(true);
    try {
      await generateGoogleTTSDialogue(activeProject.id, {
        character_name: voChar,
        text: voText,
        voice_name: voVoice,
        emotion: voEmotion,
        speaking_pace: voPace,
        track_type: 'dialogue'
      });
      setShowVoModal(false);
      await loadTracks();
      onRefreshProject();
    } catch (err: any) {
      alert('Gemini TTS generation error: ' + (err.message || err));
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
    } catch (err: any) {
      alert('Upload failed: ' + (err.message || err));
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
              Google Cloud AI Powered
            </span>
          </div>
          <p className="text-xs text-slate-400">
            Generate cinematic background scores via Lyria 3.5, synthesize character dialogue via Gemini TTS, and mix multi-track audio with sidechain ducking.
          </p>
        </div>

        <div className="flex items-center space-x-3">
          <button
            onClick={() => setShowBgmModal(true)}
            className="bg-gradient-to-r from-purple-600 to-indigo-600 hover:opacity-95 text-white px-3.5 py-2 rounded-lg text-xs font-semibold flex items-center space-x-1.5 shadow-md shadow-purple-900/20 transition-all"
          >
            <Sparkles className="w-4 h-4" />
            <span>Generate Lyria 3.5 Score</span>
          </button>

          <button
            onClick={() => setShowVoModal(true)}
            className="bg-studio-900 hover:bg-studio-800 text-cyan-300 border border-cyan-800/40 px-3.5 py-2 rounded-lg text-xs font-semibold flex items-center space-x-1.5 transition-all shadow"
          >
            <Mic className="w-4 h-4 text-cyan-400" />
            <span>Gemini TTS Dialogue</span>
          </button>

          <label className="bg-cinema-gold hover:bg-cinema-amber text-studio-950 px-4 py-2 rounded-lg text-xs font-bold flex items-center space-x-1.5 cursor-pointer shadow-md shadow-cinema-amber/10 transition-all active:scale-95">
            <Upload className="w-4 h-4 stroke-[2.5]" />
            <span>{uploading ? 'Importing...' : 'Upload Track'}</span>
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

      {/* Lyria 3.5 Music Modal */}
      {showBgmModal && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-studio-900 border border-studio-700 rounded-xl max-w-md w-full p-6 space-y-4 shadow-2xl">
            <div className="flex items-center space-x-2 text-purple-400">
              <Disc className="w-5 h-5" />
              <h3 className="text-sm font-bold uppercase tracking-wider">Generate Score via Google Lyria 3.5</h3>
            </div>
            <p className="text-xs text-slate-400">
              Synthesizes original cinematic instrumental compositions using Google's Lyria model via the Interactions API.
            </p>

            <form onSubmit={handleCreateLyriaMusic} className="space-y-3">
              <div>
                <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Score Brief / Prompt</label>
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
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Mood</label>
                  <select
                    value={bgmMood}
                    onChange={(e) => setBgmMood(e.target.value)}
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                  >
                    <option value="atmospheric cyberpunk">Atmospheric Cyberpunk</option>
                    <option value="dark orchestral tension">Dark Orchestral Tension</option>
                    <option value="ethereal sci-fi">Ethereal Deep Space</option>
                    <option value="action chase thriller">Action Chase Thriller</option>
                    <option value="melancholic noir">Melancholic Film Noir</option>
                  </select>
                </div>
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Genre</label>
                  <select
                    value={bgmGenre}
                    onChange={(e) => setBgmGenre(e.target.value)}
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                  >
                    <option value="cinematic synthwave">Cinematic Synthwave</option>
                    <option value="orchestral acoustic">Orchestral Score</option>
                    <option value="ambient drone">Ambient Texture</option>
                    <option value="hybrid electronic">Hybrid Electronic</option>
                  </select>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Tempo (BPM)</label>
                  <input
                    type="number"
                    min={60}
                    max={180}
                    value={bgmBpm}
                    onChange={(e) => setBgmBpm(Number(e.target.value))}
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white font-mono"
                  />
                </div>
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Duration (s)</label>
                  <select
                    value={bgmDuration}
                    onChange={(e) => setBgmDuration(Number(e.target.value))}
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                  >
                    <option value={15}>15 Seconds (Short Cue)</option>
                    <option value={30}>30 Seconds (Scene Score)</option>
                    <option value={60}>60 Seconds (Extended)</option>
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
                  className="bg-purple-600 hover:bg-purple-500 text-white px-4 py-1.5 rounded text-xs font-bold transition-all shadow"
                >
                  {generatingBgm ? 'Synthesizing...' : 'Generate with Lyria (~$0.05)'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Gemini TTS Dialogue Modal */}
      {showVoModal && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-studio-900 border border-studio-700 rounded-xl max-w-md w-full p-6 space-y-4 shadow-2xl">
            <div className="flex items-center space-x-2 text-cyan-400">
              <Mic className="w-5 h-5" />
              <h3 className="text-sm font-bold uppercase tracking-wider">Generate Dialogue via Gemini TTS</h3>
            </div>
            <p className="text-xs text-slate-400">
              Synthesizes expressive character voiceover using Google Gemini TTS with documented prebuilt voices.
            </p>

            <form onSubmit={handleCreateTTSDialogue} className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Speaker Name</label>
                  <input
                    type="text"
                    required
                    value={voChar}
                    onChange={(e) => setVoChar(e.target.value)}
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white font-bold uppercase"
                  />
                </div>
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Voice Profile</label>
                  <select
                    value={voVoice}
                    onChange={(e) => setVoVoice(e.target.value)}
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                  >
                    {PREBUILT_VOICES.map((v) => (
                      <option key={v.id} value={v.id}>
                        {v.label} ({v.desc.split(',')[0]})
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              <div>
                <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Dialogue Text</label>
                <textarea
                  rows={3}
                  required
                  value={voText}
                  onChange={(e) => setVoText(e.target.value)}
                  className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white font-mono"
                  placeholder="Dialogue or narration to speak..."
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Emotional Delivery</label>
                  <select
                    value={voEmotion}
                    onChange={(e) => setVoEmotion(e.target.value)}
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                  >
                    <option value="neutral">Neutral Cadence</option>
                    <option value="urgent">Urgent / Strained</option>
                    <option value="dramatic">Dramatic / Intense</option>
                    <option value="whisper">Hushed / Conspiratorial</option>
                    <option value="melancholic">Melancholic / Contemplative</option>
                  </select>
                </div>
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Pacing Modifier</label>
                  <select
                    value={voPace}
                    onChange={(e) => setVoPace(Number(e.target.value))}
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                  >
                    <option value={0.85}>0.85x (Deliberate)</option>
                    <option value={1.0}>1.0x (Standard)</option>
                    <option value={1.15}>1.15x (Fast Paced)</option>
                  </select>
                </div>
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
                  className="bg-cyan-600 hover:bg-cyan-500 text-white px-4 py-1.5 rounded text-xs font-bold transition-all shadow"
                >
                  {generatingVo ? 'Generating...' : 'Generate with Gemini TTS (~$0.01)'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Tracks List */}
      <div className="bg-studio-900 border border-studio-800 rounded-xl overflow-hidden shadow-lg">
        <div className="p-4 border-b border-studio-800 flex items-center justify-between">
          <div className="flex items-center space-x-2">
            <SlidersHorizontal className="w-4 h-4 text-slate-400" />
            <h3 className="text-xs font-bold text-white uppercase tracking-wider">Project Audio Tracks ({tracks.length})</h3>
          </div>
        </div>

        {loading ? (
          <div className="p-12 text-center text-slate-400 text-xs">Loading tracks...</div>
        ) : tracks.length === 0 ? (
          <div className="p-12 text-center space-y-2">
            <Headphones className="w-8 h-8 text-slate-600 mx-auto stroke-1" />
            <p className="text-xs text-slate-400">No audio tracks recorded yet.</p>
            <p className="text-[11px] text-slate-500">
              Use "Generate Lyria 3.5 Score" or "Gemini TTS Dialogue" above to compose audio.
            </p>
          </div>
        ) : (
          <div className="divide-y divide-studio-800/60">
            {tracks.map((track) => (
              <div
                key={track.id}
                className="p-4 flex flex-wrap items-center justify-between gap-4 hover:bg-studio-800/30 transition-colors"
              >
                <div className="flex items-center space-x-3 min-w-[280px]">
                  <button
                    onClick={() => handlePlayToggle(track)}
                    className={`w-9 h-9 rounded-full flex items-center justify-center transition-all ${
                      playingTrackId === track.id
                        ? 'bg-cinema-gold text-studio-950 shadow-md shadow-cinema-amber/20'
                        : 'bg-studio-800 text-slate-300 hover:bg-studio-700'
                    }`}
                  >
                    {playingTrackId === track.id ? (
                      <Pause className="w-4 h-4 fill-current" />
                    ) : (
                      <Play className="w-4 h-4 fill-current ml-0.5" />
                    )}
                  </button>

                  <div>
                    <div className="flex items-center space-x-2">
                      <span className="text-xs font-bold text-white tracking-tight">{track.title}</span>
                      <span
                        className={`text-[9px] px-1.5 py-0.5 rounded font-mono font-bold uppercase ${
                          track.track_type === 'music'
                            ? 'bg-purple-500/20 text-purple-300 border border-purple-500/30'
                            : 'bg-cyan-500/20 text-cyan-300 border border-cyan-500/30'
                        }`}
                      >
                        {track.track_type}
                      </span>
                    </div>
                    <div className="text-[10px] text-slate-400 font-mono mt-0.5">
                      {track.duration_seconds.toFixed(1)}s • Sidechain Ducking:{' '}
                      <button
                        onClick={() => handleDuckingToggle(track.id, track.ducking_enabled)}
                        className={`underline font-bold ${
                          track.ducking_enabled ? 'text-cinema-gold' : 'text-slate-500'
                        }`}
                      >
                        {track.ducking_enabled ? 'ON' : 'OFF'}
                      </button>
                    </div>
                  </div>
                </div>

                <div className="flex items-center space-x-6">
                  {/* Volume Slider */}
                  <div className="flex items-center space-x-2 w-36">
                    <Volume2 className="w-3.5 h-3.5 text-slate-400" />
                    <input
                      type="range"
                      min={0}
                      max={2}
                      step={0.05}
                      value={track.volume_level}
                      onChange={(e) => handleVolumeChange(track.id, parseFloat(e.target.value))}
                      className="w-full accent-cinema-gold"
                    />
                    <span className="text-[10px] font-mono text-slate-400 w-8 text-right">
                      {Math.round(track.volume_level * 100)}%
                    </span>
                  </div>

                  <button
                    onClick={() => handleDelete(track.id)}
                    className="text-slate-500 hover:text-red-400 p-1.5 rounded transition-colors"
                    title="Delete track"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};
