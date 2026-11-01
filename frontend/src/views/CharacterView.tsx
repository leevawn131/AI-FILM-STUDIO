import React, { useState, useEffect } from 'react';
import {
  Project,
  Character,
  fetchCharacters,
  createCharacter,
  generateCharacter,
  updateCharacter,
  deleteCharacter,
  uploadCharacterRef,
} from '../api/client';
import {
  Users,
  Plus,
  Sparkles,
  Trash2,
  Upload,
  User,
  Mic,
  Smile,
  Shirt,
  Copy,
  Check,
  Tag
} from 'lucide-react';

interface CharacterViewProps {
  activeProject: Project | null;
  onRefreshProject: () => void;
}

export const CharacterView: React.FC<CharacterViewProps> = ({
  activeProject,
  onRefreshProject,
}) => {
  const [characters, setCharacters] = useState<Character[]>([]);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  // AI Modal
  const [showAiModal, setShowAiModal] = useState(false);
  const [aiRole, setAiRole] = useState('Protagonist');
  const [aiConcept, setAiConcept] = useState('');

  // Manual Add Modal
  const [showAddModal, setShowAddModal] = useState(false);
  const [name, setName] = useState('');
  const [role, setRole] = useState('Protagonist');
  const [age, setAge] = useState('30s');
  const [facial, setFacial] = useState('');
  const [clothing, setClothing] = useState('');
  const [voice, setVoice] = useState('');
  const [personality, setPersonality] = useState('');

  const loadCharacters = async () => {
    if (!activeProject) return;
    setLoading(true);
    try {
      const data = await fetchCharacters(activeProject.id);
      setCharacters(data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadCharacters();
  }, [activeProject?.id]);

  const handleGenerateAi = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeProject) return;
    setGenerating(true);
    try {
      await generateCharacter(activeProject.id, aiRole, aiConcept);
      setShowAiModal(false);
      setAiConcept('');
      await loadCharacters();
      onRefreshProject();
    } catch (err: any) {
      alert('Character generation failed: ' + (err.message || err));
    } finally {
      setGenerating(false);
    }
  };

  const handleAddManual = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeProject || !name.trim()) return;
    try {
      await createCharacter({
        project_id: activeProject.id,
        name: name.trim(),
        role,
        age,
        facial_characteristics: facial,
        clothing,
        voice_profile: voice,
        personality,
        visual_prompt_tags: `${name.trim()}, ${role}, ${age}, ${clothing}, ${facial}`,
      });
      setShowAddModal(false);
      setName('');
      setFacial('');
      setClothing('');
      setVoice('');
      setPersonality('');
      await loadCharacters();
      onRefreshProject();
    } catch (err: any) {
      alert('Failed to create character: ' + (err.message || err));
    }
  };

  const handleDelete = async (id: string, charName: string) => {
    if (!window.confirm(`Delete character "${charName}"?`)) return;
    try {
      await deleteCharacter(id);
      await loadCharacters();
      onRefreshProject();
    } catch (err) {
      alert('Delete failed: ' + err);
    }
  };

  const handleFileUpload = async (charId: string, e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      await uploadCharacterRef(charId, file);
      await loadCharacters();
    } catch (err) {
      alert('Image upload failed: ' + err);
    }
  };

  const handleCopyTags = (char: Character) => {
    navigator.clipboard.writeText(char.visual_prompt_tags);
    setCopiedId(char.id);
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
            <h2 className="text-xl font-bold text-white tracking-tight">Character Consistency Database</h2>
            <span className="text-[10px] bg-cinema-amber/10 text-cinema-gold border border-cinema-amber/30 px-2 py-0.5 rounded font-mono font-medium">
              Flow Anchor Locked
            </span>
          </div>
          <p className="text-xs text-slate-400">
            Maintain strict facial, wardrobe, and voice consistency across all generated video scenes
          </p>
        </div>

        <div className="flex items-center space-x-3">
          <button
            onClick={() => setShowAiModal(true)}
            className="bg-gradient-to-r from-cinema-amber to-cinema-gold text-studio-950 px-4 py-2 rounded-lg text-xs font-bold flex items-center space-x-1.5 shadow-md shadow-cinema-amber/10 transition-all"
          >
            <Sparkles className="w-4 h-4 stroke-[2.5]" />
            <span>Character Designer Agent</span>
          </button>

          <button
            onClick={() => setShowAddModal(true)}
            className="bg-studio-900 hover:bg-studio-800 text-slate-200 border border-studio-700 px-4 py-2 rounded-lg text-xs font-semibold flex items-center space-x-1.5 transition-all"
          >
            <Plus className="w-4 h-4" />
            <span>Add Character</span>
          </button>
        </div>
      </div>

      {/* AI Generator Modal */}
      {showAiModal && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-studio-900 border border-studio-700 rounded-xl max-w-md w-full p-6 space-y-4 shadow-2xl">
            <div className="flex items-center space-x-2 text-cinema-gold">
              <Sparkles className="w-5 h-5" />
              <h3 className="text-sm font-bold uppercase tracking-wider">AI Character Designer</h3>
            </div>
            <p className="text-xs text-slate-400">
              Generates consistency-locked facial traits, master wardrobe, voice profile, and Flow anchor tokens.
            </p>

            <form onSubmit={handleGenerateAi} className="space-y-3">
              <div>
                <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Narrative Role</label>
                <select
                  value={aiRole}
                  onChange={(e) => setAiRole(e.target.value)}
                  className="w-full bg-studio-950 border border-studio-700 rounded px-3 py-1.5 text-xs text-white"
                >
                  <option value="Protagonist">Protagonist</option>
                  <option value="Antagonist">Antagonist / Rival</option>
                  <option value="Supporting Specialist">Supporting Specialist</option>
                  <option value="Mentor">Mentor / Guide</option>
                  <option value="Enforcer">Enforcer / Operative</option>
                </select>
              </div>

              <div>
                <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">
                  Concept Hint / Archetype (Optional)
                </label>
                <textarea
                  rows={2}
                  value={aiConcept}
                  onChange={(e) => setAiConcept(e.target.value)}
                  placeholder="e.g. Rogue cyber-surgeon haunted by an experimental neural implant..."
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
                  className="bg-cinema-gold text-studio-950 px-4 py-1.5 rounded text-xs font-bold"
                >
                  {generating ? 'Designing...' : 'Generate Profile'}
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
            <h3 className="text-sm font-bold text-white uppercase tracking-wider">Create Master Character Sheet</h3>
            <form onSubmit={handleAddManual} className="space-y-3">
              <div className="grid grid-cols-3 gap-3">
                <div className="col-span-2">
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Character Name *</label>
                  <input
                    type="text"
                    required
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="e.g. KAI CROSS"
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white uppercase font-bold"
                  />
                </div>
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Age</label>
                  <input
                    type="text"
                    value={age}
                    onChange={(e) => setAge(e.target.value)}
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                  />
                </div>
              </div>

              <div>
                <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Narrative Role</label>
                <input
                  type="text"
                  value={role}
                  onChange={(e) => setRole(e.target.value)}
                  className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                />
              </div>

              <div>
                <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">
                  Facial Characteristics (Locked Anchor)
                </label>
                <textarea
                  rows={2}
                  value={facial}
                  onChange={(e) => setFacial(e.target.value)}
                  placeholder="Sharp jawline, titanium ocular eye graft, faint scar over temple..."
                  className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                />
              </div>

              <div>
                <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">
                  Master Wardrobe & Outfit
                </label>
                <textarea
                  rows={2}
                  value={clothing}
                  onChange={(e) => setClothing(e.target.value)}
                  placeholder="Distressed carbon-fiber trench coat, slate tactical turtleneck..."
                  className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Voice Profile</label>
                  <input
                    type="text"
                    value={voice}
                    onChange={(e) => setVoice(e.target.value)}
                    placeholder="Deep baritone, quiet intensity..."
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                  />
                </div>
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Personality</label>
                  <input
                    type="text"
                    value={personality}
                    onChange={(e) => setPersonality(e.target.value)}
                    placeholder="Stoic, analytical..."
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                  />
                </div>
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
                  Save Character
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Characters Cards Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {characters.map((char) => {
          const isCopied = copiedId === char.id;
          const refImgUrl = char.reference_image_path ? `/media/${char.reference_image_path}` : null;

          return (
            <div
              key={char.id}
              className="rounded-xl bg-studio-900 border border-studio-800 overflow-hidden shadow-lg flex flex-col justify-between hover:border-studio-700 transition-all"
            >
              {/* Card Header & Avatar */}
              <div className="p-5 space-y-4">
                <div className="flex items-start justify-between">
                  <div className="flex items-center space-x-3">
                    <div className="relative w-14 h-14 rounded-xl bg-studio-950 border border-studio-700 overflow-hidden flex items-center justify-center shrink-0">
                      {refImgUrl ? (
                        <img src={refImgUrl} alt={char.name} className="w-full h-full object-cover" />
                      ) : (
                        <User className="w-7 h-7 text-slate-600" />
                      )}
                      <label className="absolute inset-0 bg-black/60 opacity-0 hover:opacity-100 flex items-center justify-center cursor-pointer transition-opacity">
                        <Upload className="w-4 h-4 text-cinema-gold" />
                        <input
                          type="file"
                          accept="image/*"
                          className="hidden"
                          onChange={(e) => handleFileUpload(char.id, e)}
                        />
                      </label>
                    </div>

                    <div>
                      <h3 className="text-base font-bold text-white tracking-wide uppercase">{char.name}</h3>
                      <div className="flex items-center space-x-2 mt-0.5">
                        <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-studio-950 text-cyan-400 border border-cyan-800/30">
                          {char.role}
                        </span>
                        <span className="text-[10px] text-slate-400 font-mono">Age: {char.age}</span>
                      </div>
                    </div>
                  </div>

                  <button
                    onClick={() => handleDelete(char.id, char.name)}
                    className="text-slate-500 hover:text-cinema-crimson p-1 transition-colors"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>

                {/* Consistency Spec Blocks */}
                <div className="space-y-2.5 text-xs">
                  <div className="bg-studio-950 p-2.5 rounded-lg border border-studio-800/80">
                    <div className="flex items-center space-x-1.5 text-slate-400 font-mono text-[10px] uppercase mb-1">
                      <User className="w-3 h-3 text-cinema-gold" />
                      <span>Facial Features</span>
                    </div>
                    <p className="text-slate-200 line-clamp-2">{char.facial_characteristics || 'Not specified'}</p>
                  </div>

                  <div className="bg-studio-950 p-2.5 rounded-lg border border-studio-800/80">
                    <div className="flex items-center space-x-1.5 text-slate-400 font-mono text-[10px] uppercase mb-1">
                      <Shirt className="w-3 h-3 text-cyan-400" />
                      <span>Master Clothing Anchor</span>
                    </div>
                    <p className="text-slate-200 line-clamp-2">{char.clothing || 'Not specified'}</p>
                  </div>

                  <div className="grid grid-cols-2 gap-2 text-[11px]">
                    <div className="bg-studio-950 p-2 rounded border border-studio-800/80">
                      <div className="flex items-center space-x-1 text-slate-400 font-mono text-[9px] uppercase mb-0.5">
                        <Mic className="w-3 h-3 text-purple-400" />
                        <span>Voice Profile</span>
                      </div>
                      <p className="text-slate-300 truncate">{char.voice_profile || 'Standard'}</p>
                    </div>

                    <div className="bg-studio-950 p-2 rounded border border-studio-800/80">
                      <div className="flex items-center space-x-1 text-slate-400 font-mono text-[9px] uppercase mb-0.5">
                        <Smile className="w-3 h-3 text-emerald-400" />
                        <span>Personality</span>
                      </div>
                      <p className="text-slate-300 truncate">{char.personality || 'Neutral'}</p>
                    </div>
                  </div>
                </div>

                {/* Flow Visual Prompt Anchor */}
                <div className="bg-studio-950 p-2.5 rounded-lg border border-studio-800/80 space-y-1">
                  <div className="flex items-center justify-between text-[10px] font-mono text-cyan-400">
                    <span className="flex items-center space-x-1">
                      <Tag className="w-3 h-3" />
                      <span>Google Flow Consistency Tokens</span>
                    </span>
                    <button
                      onClick={() => handleCopyTags(char)}
                      className="text-cinema-gold hover:text-white flex items-center space-x-1"
                    >
                      {isCopied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
                      <span>{isCopied ? 'Copied' : 'Copy'}</span>
                    </button>
                  </div>
                  <p className="text-[11px] text-slate-300 font-mono line-clamp-2">{char.visual_prompt_tags}</p>
                </div>
              </div>

              {/* Footer action */}
              <div className="px-5 py-3 bg-studio-950/60 border-t border-studio-800 flex items-center justify-between text-xs">
                <label className="text-cinema-gold hover:text-white flex items-center space-x-1 cursor-pointer font-medium transition-colors">
                  <Upload className="w-3.5 h-3.5" />
                  <span>Upload Reference Image</span>
                  <input
                    type="file"
                    accept="image/*"
                    className="hidden"
                    onChange={(e) => handleFileUpload(char.id, e)}
                  />
                </label>
              </div>
            </div>
          );
        })}

        {characters.length === 0 && !loading && (
          <div className="col-span-3 p-12 text-center text-slate-500 text-xs font-mono space-y-3">
            <Users className="w-8 h-8 text-slate-600 mx-auto" />
            <p>No characters created yet.</p>
            <p className="text-slate-400">
              Click <strong className="text-cinema-gold">"Character Designer Agent"</strong> above to auto-create consistent character sheets.
            </p>
          </div>
        )}
      </div>
    </div>
  );
};
