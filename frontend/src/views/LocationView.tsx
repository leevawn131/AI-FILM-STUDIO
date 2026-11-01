import React, { useState, useEffect } from 'react';
import {
  Project,
  Location,
  fetchLocations,
  createLocation,
  generateLocation,
  deleteLocation,
  uploadLocationRef,
} from '../api/client';
import {
  MapPin,
  Plus,
  Sparkles,
  Trash2,
  Upload,
  Sun,
  CloudRain,
  Lightbulb,
  Copy,
  Check,
  Tag
} from 'lucide-react';

interface LocationViewProps {
  activeProject: Project | null;
  onRefreshProject: () => void;
}

export const LocationView: React.FC<LocationViewProps> = ({
  activeProject,
  onRefreshProject,
}) => {
  const [locations, setLocations] = useState<Location[]>([]);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  // AI Modal
  const [showAiModal, setShowAiModal] = useState(false);
  const [aiTime, setAiTime] = useState('Night');
  const [aiAtmosphere, setAiAtmosphere] = useState('');

  // Manual Add Modal
  const [showAddModal, setShowAddModal] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [timeOfDay, setTimeOfDay] = useState('Night');
  const [weather, setWeather] = useState('Clear');
  const [lighting, setLighting] = useState('High contrast cinematic lighting with rim highlights');

  const loadLocations = async () => {
    if (!activeProject) return;
    setLoading(true);
    try {
      const data = await fetchLocations(activeProject.id);
      setLocations(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadLocations();
  }, [activeProject?.id]);

  const handleGenerateAi = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeProject) return;
    setGenerating(true);
    try {
      await generateLocation(activeProject.id, aiTime, aiAtmosphere);
      setShowAiModal(false);
      setAiAtmosphere('');
      await loadLocations();
      onRefreshProject();
    } catch (err: any) {
      alert('Location generation failed: ' + (err.message || err));
    } finally {
      setGenerating(false);
    }
  };

  const handleAddManual = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeProject || !name.trim()) return;
    try {
      await createLocation({
        project_id: activeProject.id,
        name: name.trim(),
        description,
        time_of_day: timeOfDay,
        weather,
        lighting_setup: lighting,
        visual_prompt_tags: `${name.trim()}, ${timeOfDay}, ${weather}, ${lighting}`,
      });
      setShowAddModal(false);
      setName('');
      setDescription('');
      await loadLocations();
      onRefreshProject();
    } catch (err: any) {
      alert('Failed to create location: ' + (err.message || err));
    }
  };

  const handleDelete = async (id: string, locName: string) => {
    if (!window.confirm(`Delete location "${locName}"?`)) return;
    try {
      await deleteLocation(id);
      await loadLocations();
      onRefreshProject();
    } catch (err) {
      alert('Delete failed: ' + err);
    }
  };

  const handleFileUpload = async (locId: string, e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      await uploadLocationRef(locId, file);
      await loadLocations();
    } catch (err) {
      alert('Upload failed: ' + err);
    }
  };

  const handleCopyTags = (loc: Location) => {
    navigator.clipboard.writeText(loc.visual_prompt_tags);
    setCopiedId(loc.id);
    setTimeout(() => setCopiedId(null), 2000);
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
            <h2 className="text-xl font-bold text-white tracking-tight">Location & Set Database</h2>
            <span className="text-[10px] bg-cyan-500/10 text-cyan-400 border border-cyan-500/30 px-2 py-0.5 rounded font-mono font-medium">
              Atmosphere & Lighting Anchors
            </span>
          </div>
          <p className="text-xs text-slate-400">
            Define sets, environmental conditions, and volumetric lighting profiles for Google Flow
          </p>
        </div>

        <div className="flex items-center space-x-3">
          <button
            onClick={() => setShowAiModal(true)}
            className="bg-gradient-to-r from-cyan-600 to-blue-600 text-white px-4 py-2 rounded-lg text-xs font-bold flex items-center space-x-1.5 shadow-md shadow-cyan-900/20 transition-all"
          >
            <Sparkles className="w-4 h-4 stroke-[2.5]" />
            <span>AI Location Generator</span>
          </button>

          <button
            onClick={() => setShowAddModal(true)}
            className="bg-studio-900 hover:bg-studio-800 text-slate-200 border border-studio-700 px-4 py-2 rounded-lg text-xs font-semibold flex items-center space-x-1.5 transition-all"
          >
            <Plus className="w-4 h-4" />
            <span>Add Location</span>
          </button>
        </div>
      </div>

      {/* AI Modal */}
      {showAiModal && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-studio-900 border border-studio-700 rounded-xl max-w-md w-full p-6 space-y-4 shadow-2xl">
            <div className="flex items-center space-x-2 text-cyan-400">
              <Sparkles className="w-5 h-5" />
              <h3 className="text-sm font-bold uppercase tracking-wider">AI Set Designer</h3>
            </div>
            <p className="text-xs text-slate-400">
              Generates atmospheric sets with lighting profiles and Google Flow background prompt anchors.
            </p>

            <form onSubmit={handleGenerateAi} className="space-y-3">
              <div>
                <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Time of Day</label>
                <select
                  value={aiTime}
                  onChange={(e) => setAiTime(e.target.value)}
                  className="w-full bg-studio-950 border border-studio-700 rounded px-3 py-1.5 text-xs text-white"
                >
                  <option value="Night">Night (Deep shadows & artificial lights)</option>
                  <option value="Midnight">Midnight (Extreme chiaroscuro)</option>
                  <option value="Golden Hour">Golden Hour (Warm diffused low angle sun)</option>
                  <option value="Twilight / Blue Hour">Twilight / Blue Hour (Moody cyan hues)</option>
                  <option value="Overcast Dawn">Overcast Dawn (Muted desaturated light)</option>
                  <option value="High Noon">High Noon (Harsh overhead glare)</option>
                </select>
              </div>

              <div>
                <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">
                  Atmosphere / World Context (Optional)
                </label>
                <textarea
                  rows={2}
                  value={aiAtmosphere}
                  onChange={(e) => setAiAtmosphere(e.target.value)}
                  placeholder="e.g. Submerged data vault under an ocean spire with neon coolant leaks..."
                  className="w-full bg-studio-950 border border-studio-700 rounded px-3 py-1.5 text-xs text-white"
                />
              </div>

              <div className="flex justify-end space-x-2 pt-2 border-t border-studio-800">
                <button
                  type="button"
                  onClick={() => setShowAiModal(false)}
                  className="px-3 py-1.5 rounded text-xs text-slate-400 hover:text-white"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={generating}
                  className="bg-cyan-500 hover:bg-cyan-400 text-studio-950 px-4 py-1.5 rounded text-xs font-bold"
                >
                  {generating ? 'Constructing...' : 'Generate Set'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Manual Add Modal */}
      {showAddModal && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-studio-900 border border-studio-700 rounded-xl max-w-lg w-full p-6 space-y-4 shadow-2xl">
            <h3 className="text-sm font-bold text-white uppercase tracking-wider">Create Location Master</h3>
            <form onSubmit={handleAddManual} className="space-y-3">
              <div>
                <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Set / Location Name *</label>
                <input
                  type="text"
                  required
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="e.g. SPIRE 9 CORE TERMINAL"
                  className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white font-bold"
                />
              </div>

              <div>
                <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Description</label>
                <textarea
                  rows={2}
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="Architectural layout, materials, mood..."
                  className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Time of Day</label>
                  <input
                    type="text"
                    value={timeOfDay}
                    onChange={(e) => setTimeOfDay(e.target.value)}
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                  />
                </div>
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Weather / Haze</label>
                  <input
                    type="text"
                    value={weather}
                    onChange={(e) => setWeather(e.target.value)}
                    placeholder="Neon rain, heavy fog..."
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                  />
                </div>
              </div>

              <div>
                <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Lighting Setup</label>
                <textarea
                  rows={2}
                  value={lighting}
                  onChange={(e) => setLighting(e.target.value)}
                  placeholder="Volumetric cyan rim lighting, caustic water reflections..."
                  className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                />
              </div>

              <div className="flex justify-end space-x-2 pt-2 border-t border-studio-800">
                <button
                  type="button"
                  onClick={() => setShowAddModal(false)}
                  className="px-3 py-1.5 rounded text-xs text-slate-400 hover:text-white"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="bg-cinema-gold text-studio-950 px-4 py-1.5 rounded text-xs font-bold"
                >
                  Save Set
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Locations Cards Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {locations.map((loc) => {
          const isCopied = copiedId === loc.id;
          const refImgUrl = loc.reference_image_path ? `/media/${loc.reference_image_path}` : null;

          return (
            <div
              key={loc.id}
              className="rounded-xl bg-studio-900 border border-studio-800 overflow-hidden shadow-lg flex flex-col justify-between hover:border-studio-700 transition-all"
            >
              <div className="p-5 space-y-4">
                <div className="flex items-start justify-between">
                  <div className="flex items-center space-x-3">
                    <div className="relative w-14 h-14 rounded-xl bg-studio-950 border border-studio-700 overflow-hidden flex items-center justify-center shrink-0">
                      {refImgUrl ? (
                        <img src={refImgUrl} alt={loc.name} className="w-full h-full object-cover" />
                      ) : (
                        <MapPin className="w-7 h-7 text-slate-600" />
                      )}
                      <label className="absolute inset-0 bg-black/60 opacity-0 hover:opacity-100 flex items-center justify-center cursor-pointer transition-opacity">
                        <Upload className="w-4 h-4 text-cinema-gold" />
                        <input
                          type="file"
                          accept="image/*"
                          className="hidden"
                          onChange={(e) => handleFileUpload(loc.id, e)}
                        />
                      </label>
                    </div>

                    <div>
                      <h3 className="text-base font-bold text-white tracking-wide">{loc.name}</h3>
                      <div className="flex items-center space-x-2 mt-0.5">
                        <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-studio-950 text-cinema-gold border border-cinema-gold/30">
                          {loc.time_of_day}
                        </span>
                        <span className="text-[10px] text-slate-400 font-mono truncate max-w-[120px]">
                          {loc.weather}
                        </span>
                      </div>
                    </div>
                  </div>

                  <button
                    onClick={() => handleDelete(loc.id, loc.name)}
                    className="text-slate-500 hover:text-cinema-crimson p-1 transition-colors"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>

                <p className="text-xs text-slate-300 leading-relaxed">{loc.description}</p>

                {/* Lighting Setup Block */}
                <div className="bg-studio-950 p-2.5 rounded-lg border border-studio-800/80">
                  <div className="flex items-center space-x-1.5 text-slate-400 font-mono text-[10px] uppercase mb-1">
                    <Lightbulb className="w-3 h-3 text-cinema-gold" />
                    <span>Lighting Setup</span>
                  </div>
                  <p className="text-slate-200 text-xs">{loc.lighting_setup}</p>
                </div>

                {/* Flow Visual Prompt Tokens */}
                <div className="bg-studio-950 p-2.5 rounded-lg border border-studio-800/80 space-y-1">
                  <div className="flex items-center justify-between text-[10px] font-mono text-cyan-400">
                    <span className="flex items-center space-x-1">
                      <Tag className="w-3 h-3" />
                      <span>Flow Background Tokens</span>
                    </span>
                    <button
                      onClick={() => handleCopyTags(loc)}
                      className="text-cinema-gold hover:text-white flex items-center space-x-1"
                    >
                      {isCopied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
                      <span>{isCopied ? 'Copied' : 'Copy'}</span>
                    </button>
                  </div>
                  <p className="text-[11px] text-slate-300 font-mono line-clamp-2">{loc.visual_prompt_tags}</p>
                </div>
              </div>

              <div className="px-5 py-3 bg-studio-950/60 border-t border-studio-800 flex items-center justify-between text-xs">
                <label className="text-cyan-400 hover:text-white flex items-center space-x-1 cursor-pointer font-medium transition-colors">
                  <Upload className="w-3.5 h-3.5" />
                  <span>Upload Set Reference Image</span>
                  <input
                    type="file"
                    accept="image/*"
                    className="hidden"
                    onChange={(e) => handleFileUpload(loc.id, e)}
                  />
                </label>
              </div>
            </div>
          );
        })}

        {locations.length === 0 && !loading && (
          <div className="col-span-3 p-12 text-center text-slate-500 text-xs font-mono space-y-3">
            <MapPin className="w-8 h-8 text-slate-600 mx-auto" />
            <p>No locations created yet.</p>
            <p className="text-slate-400">
              Click <strong className="text-cinema-gold">"AI Location Generator"</strong> to design realistic studio sets.
            </p>
          </div>
        )}
      </div>
    </div>
  );
};
