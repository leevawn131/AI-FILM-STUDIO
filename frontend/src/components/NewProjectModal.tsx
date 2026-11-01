import React, { useState } from 'react';
import { X, Clapperboard, Sparkles } from 'lucide-react';
import { Project } from '../api/client';

interface NewProjectModalProps {
  isOpen: boolean;
  onClose: () => void;
  onCreate: (data: Partial<Project>) => Promise<void>;
}

const GENRE_PRESETS = [
  { name: 'Cyberpunk', style: 'Neon reflections, 35mm anamorphic lens, rain-slicked asphalt, high contrast chiaroscuro' },
  { name: 'Sci-Fi', style: 'Hyper-detailed starship corridors, cyan rim lighting, volumetric star flares, deep space atmosphere' },
  { name: 'Noir / Mystery', style: 'High contrast black and white / deep shadows, Venetian blind slats, smoke swirls, 1940s lighting' },
  { name: 'Action / Thriller', style: 'Crisp shutter angle, dynamic handheld motion, high dynamic range lens flares, gritty realism' },
  { name: 'Drama / Indie', style: 'A24 soft golden hour lighting, 16mm organic film grain, shallow depth of field f/1.4' },
];

export const NewProjectModal: React.FC<NewProjectModalProps> = ({ isOpen, onClose, onCreate }) => {
  const [name, setName] = useState('');
  const [logline, setLogline] = useState('');
  const [genre, setGenre] = useState('Cyberpunk');
  const [aspectRatio, setAspectRatio] = useState('2.39:1');
  const [fps, setFps] = useState(24);
  const [targetDuration, setTargetDuration] = useState(120);
  const [visualStyle, setVisualStyle] = useState(GENRE_PRESETS[0].style);
  const [budgetTotal, setBudgetTotal] = useState(100.0);
  const [loading, setLoading] = useState(false);

  if (!isOpen) return null;

  const handleGenreChange = (newGenre: string) => {
    setGenre(newGenre);
    const preset = GENRE_PRESETS.find((g) => g.name === newGenre);
    if (preset) {
      setVisualStyle(preset.style);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setLoading(true);
    try {
      await onCreate({
        name: name.trim(),
        logline: logline.trim(),
        genre,
        aspect_ratio: aspectRatio,
        fps,
        target_duration_sec: targetDuration,
        visual_style: visualStyle,
        budget_total: budgetTotal,
      });
      onClose();
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
      <div className="bg-studio-900 border border-studio-700 rounded-xl w-full max-w-2xl overflow-hidden shadow-2xl">
        {/* Modal Header */}
        <div className="px-6 py-4 border-b border-studio-800 flex items-center justify-between bg-studio-950/60">
          <div className="flex items-center space-x-2.5">
            <div className="w-8 h-8 rounded bg-cinema-amber/20 text-cinema-gold flex items-center justify-center">
              <Clapperboard className="w-4 h-4" />
            </div>
            <div>
              <h2 className="text-base font-bold text-white">Create New Film Production</h2>
              <p className="text-xs text-slate-400">Initialize a new screenplay, storyboard, and Flow handoff project</p>
            </div>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-white transition-colors">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Modal Form */}
        <form onSubmit={handleSubmit} className="p-6 space-y-4 max-h-[80vh] overflow-y-auto">
          {/* Film Title */}
          <div>
            <label className="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-1">
              Film Title *
            </label>
            <input
              type="text"
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Obsidian Protocol: Genesis"
              className="w-full bg-studio-950 border border-studio-700 rounded-lg px-3.5 py-2 text-sm text-white focus:outline-none focus:border-cinema-gold"
            />
          </div>

          {/* Logline */}
          <div>
            <label className="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-1">
              Logline / Premise
            </label>
            <textarea
              rows={2}
              value={logline}
              onChange={(e) => setLogline(e.target.value)}
              placeholder="A one-sentence summary of the protagonist, central conflict, and world stakes..."
              className="w-full bg-studio-950 border border-studio-700 rounded-lg px-3.5 py-2 text-xs text-white focus:outline-none focus:border-cinema-gold"
            />
          </div>

          {/* Genre and Aspect Ratio */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-1">
                Cinematic Genre
              </label>
              <select
                value={genre}
                onChange={(e) => handleGenreChange(e.target.value)}
                className="w-full bg-studio-950 border border-studio-700 rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-cinema-gold"
              >
                {GENRE_PRESETS.map((g) => (
                  <option key={g.name} value={g.name}>
                    {g.name}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-1">
                Aspect Ratio
              </label>
              <select
                value={aspectRatio}
                onChange={(e) => setAspectRatio(e.target.value)}
                className="w-full bg-studio-950 border border-studio-700 rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-cinema-gold"
              >
                <option value="2.39:1">2.39:1 (Anamorphic Cinemascope)</option>
                <option value="16:9">16:9 (Standard Cinematic Widescreen)</option>
                <option value="9:16">9:16 (Vertical Short-form / Mobile)</option>
                <option value="4:3">4:3 (Vintage Academy Ratio)</option>
              </select>
            </div>
          </div>

          {/* Technical Specs: FPS, Target Duration, Budget */}
          <div className="grid grid-cols-3 gap-4">
            <div>
              <label className="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-1">
                Frame Rate (FPS)
              </label>
              <select
                value={fps}
                onChange={(e) => setFps(Number(e.target.value))}
                className="w-full bg-studio-950 border border-studio-700 rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-cinema-gold font-mono"
              >
                <option value={24}>24 FPS (Film Motion)</option>
                <option value={30}>30 FPS (Video)</option>
                <option value={60}>60 FPS (HFR Smooth)</option>
              </select>
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-1">
                Target Length (sec)
              </label>
              <input
                type="number"
                min={15}
                max={3600}
                value={targetDuration}
                onChange={(e) => setTargetDuration(Number(e.target.value))}
                className="w-full bg-studio-950 border border-studio-700 rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-cinema-gold font-mono"
              />
            </div>

            <div>
              <label className="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-1">
                Budget Cap ($)
              </label>
              <input
                type="number"
                min={5}
                step={5}
                value={budgetTotal}
                onChange={(e) => setBudgetTotal(Number(e.target.value))}
                className="w-full bg-studio-950 border border-studio-700 rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-cinema-gold font-mono"
              />
            </div>
          </div>

          {/* Visual Style Prompt Anchor */}
          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="block text-xs font-semibold text-slate-300 uppercase tracking-wider">
                Visual Style Anchor (Google Flow Token)
              </label>
              <span className="text-[10px] text-cinema-cyan flex items-center space-x-1">
                <Sparkles className="w-3 h-3" />
                <span>Cinematographer Prompt Anchor</span>
              </span>
            </div>
            <textarea
              rows={2}
              value={visualStyle}
              onChange={(e) => setVisualStyle(e.target.value)}
              className="w-full bg-studio-950 border border-studio-700 rounded-lg px-3.5 py-2 text-xs text-slate-200 focus:outline-none focus:border-cinema-gold font-mono"
            />
          </div>

          {/* Footer actions */}
          <div className="pt-4 border-t border-studio-800 flex items-center justify-end space-x-3">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 rounded-lg text-xs font-semibold text-slate-300 hover:text-white transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={loading || !name.trim()}
              className="bg-gradient-to-r from-cinema-amber to-cinema-gold hover:opacity-95 text-studio-950 px-5 py-2 rounded-lg text-xs font-bold shadow-lg shadow-cinema-amber/15 transition-all disabled:opacity-50"
            >
              {loading ? 'Creating Project...' : 'Initialize Studio Project'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
