import React, { useState, useEffect } from 'react';
import {
  Project,
  Scene,
  Shot,
  fetchScenes,
  fetchShots,
  createShot,
  generateShotsForScene,
  updateShot,
  deleteShot,
  optimizeShotPrompt,
  runContinuityCheck,
  ContinuityReport
} from '../api/client';
import {
  Layers,
  Sparkles,
  Camera,
  Play,
  Copy,
  Check,
  Plus,
  Trash2,
  Wand2,
  ShieldAlert,
  ShieldCheck,
  Video,
  Clock,
  Eye,
  Settings
} from 'lucide-react';

interface StoryboardViewProps {
  activeProject: Project | null;
  onRefreshProject: () => void;
}

export const StoryboardView: React.FC<StoryboardViewProps> = ({
  activeProject,
  onRefreshProject,
}) => {
  const [scenes, setScenes] = useState<Scene[]>([]);
  const [selectedSceneId, setSelectedSceneId] = useState<string>('');
  const [shots, setShots] = useState<Shot[]>([]);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [shotCountToGen, setShotCountToGen] = useState(4);
  const [copiedShotId, setCopiedShotId] = useState<string | null>(null);

  // Continuity check modal & state
  const [continuityReport, setContinuityReport] = useState<ContinuityReport | null>(null);
  const [checkingContinuity, setCheckingContinuity] = useState(false);
  const [showContinuityModal, setShowContinuityModal] = useState(false);

  // Add Shot Modal
  const [showAddModal, setShowAddModal] = useState(false);
  const [newAngle, setNewAngle] = useState('Medium Two-Shot');
  const [newMovement, setNewMovement] = useState('Slow Dolly Push In');
  const [newDuration, setNewDuration] = useState(4.0);
  const [newDesc, setNewDesc] = useState('');
  const [newLighting, setNewLighting] = useState('High contrast chiaroscuro');
  const [newPrompt, setNewPrompt] = useState('');

  const loadScenesAndShots = async () => {
    if (!activeProject) return;
    setLoading(true);
    try {
      const scList = await fetchScenes(activeProject.id);
      setScenes(scList);
      const activeScId = selectedSceneId || (scList.length > 0 ? scList[0].id : '');
      if (!selectedSceneId && scList.length > 0) {
        setSelectedSceneId(scList[0].id);
      }
      const shList = await fetchShots(activeProject.id, activeScId || undefined);
      setShots(shList);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadScenesAndShots();
  }, [activeProject?.id, selectedSceneId]);

  const handleGenerateShots = async () => {
    if (!selectedSceneId) {
      alert('Please select a scene first.');
      return;
    }
    setGenerating(true);
    try {
      await generateShotsForScene(selectedSceneId, shotCountToGen);
      await loadScenesAndShots();
      onRefreshProject();
    } catch (err: any) {
      alert('Director breakdown failed: ' + (err.message || err));
    } finally {
      setGenerating(false);
    }
  };

  const handleCopyPrompt = (shot: Shot) => {
    navigator.clipboard.writeText(shot.video_prompt);
    setCopiedShotId(shot.id);
    setTimeout(() => setCopiedShotId(null), 2000);
  };

  const handleStatusChange = async (shotId: string, status: any) => {
    try {
      await updateShot(shotId, { generation_status: status });
      await loadScenesAndShots();
      onRefreshProject();
    } catch (err) {
      alert('Failed to update status: ' + err);
    }
  };

  const handleOptimizePrompt = async (shotId: string) => {
    try {
      await optimizeShotPrompt(shotId);
      await loadScenesAndShots();
    } catch (err) {
      alert('Cinematographer optimization failed: ' + err);
    }
  };

  const handleDeleteShot = async (shotId: string) => {
    if (!window.confirm('Delete this storyboard shot?')) return;
    try {
      await deleteShot(shotId);
      await loadScenesAndShots();
      onRefreshProject();
    } catch (err) {
      alert('Failed to delete shot: ' + err);
    }
  };

  const handleRunContinuityAudit = async () => {
    if (!activeProject) return;
    setCheckingContinuity(true);
    try {
      const rep = await runContinuityCheck(activeProject.id);
      setContinuityReport(rep);
      setShowContinuityModal(true);
    } catch (err) {
      alert('Continuity check failed: ' + err);
    } finally {
      setCheckingContinuity(false);
    }
  };

  const handleAddManualShot = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!activeProject || !selectedSceneId) return;
    try {
      await createShot({
        project_id: activeProject.id,
        scene_id: selectedSceneId,
        camera_angle: newAngle,
        camera_movement: newMovement,
        duration_seconds: newDuration,
        visual_description: newDesc,
        lighting: newLighting,
        video_prompt: newPrompt || `${newAngle}, ${newMovement}. ${newDesc}. Lighting: ${newLighting}.`,
        generation_status: 'ready_for_flow',
      });
      setShowAddModal(false);
      setNewDesc('');
      setNewPrompt('');
      await loadScenesAndShots();
      onRefreshProject();
    } catch (err) {
      alert('Failed to create shot: ' + err);
    }
  };

  if (!activeProject) {
    return <div className="p-8 text-center text-slate-400">Please select a project first.</div>;
  }

  const selectedScene = scenes.find((s) => s.id === selectedSceneId);

  return (
    <div className="p-8 space-y-6 max-w-7xl mx-auto">
      {/* View Header */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="flex items-center space-x-2">
            <h2 className="text-xl font-bold text-white tracking-tight">Storyboard & Shot List</h2>
            <span className="text-[10px] bg-cyan-500/10 text-cyan-400 border border-cyan-500/30 px-2 py-0.5 rounded font-mono font-medium">
              Google Flow Ready
            </span>
          </div>
          <p className="text-xs text-slate-400">
            Director camera blocking, Cinematographer lighting profiles, and Google Flow prompt sheets
          </p>
        </div>

        {/* Top actions */}
        <div className="flex items-center space-x-3">
          <button
            onClick={handleRunContinuityAudit}
            disabled={checkingContinuity}
            className="bg-studio-900 hover:bg-studio-800 text-slate-200 border border-studio-700 px-3.5 py-2 rounded-lg text-xs font-semibold flex items-center space-x-1.5 transition-all shadow"
          >
            <ShieldCheck className="w-4 h-4 text-emerald-400" />
            <span>{checkingContinuity ? 'Auditing...' : 'Continuity Audit'}</span>
          </button>

          <button
            onClick={() => setShowAddModal(true)}
            className="bg-studio-900 hover:bg-studio-800 text-cinema-gold border border-cinema-gold/30 px-3.5 py-2 rounded-lg text-xs font-semibold flex items-center space-x-1.5 transition-all"
          >
            <Plus className="w-4 h-4" />
            <span>Add Shot</span>
          </button>
        </div>
      </div>

      {/* Scene Selector & Director Agent Generator Bar */}
      <div className="p-4 rounded-xl bg-studio-900 border border-studio-800 flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center space-x-3">
          <span className="text-xs font-semibold uppercase font-mono text-slate-400">Filter Scene:</span>
          <select
            value={selectedSceneId}
            onChange={(e) => setSelectedSceneId(e.target.value)}
            className="bg-studio-950 border border-studio-700 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none focus:border-cinema-gold"
          >
            <option value="">All Scenes ({scenes.length})</option>
            {scenes.map((s) => (
              <option key={s.id} value={s.id}>
                Scene {s.scene_number}: {s.slugline}
              </option>
            ))}
          </select>

          {selectedScene && (
            <span className="text-xs text-slate-400 font-mono hidden md:inline">
              Est. {selectedScene.estimated_duration}s • Budget: ${selectedScene.budget_limit}
            </span>
          )}
        </div>

        {selectedSceneId && (
          <div className="flex items-center space-x-2">
            <span className="text-xs font-mono text-slate-400">Shots to generate:</span>
            <input
              type="number"
              min={1}
              max={8}
              value={shotCountToGen}
              onChange={(e) => setShotCountToGen(Number(e.target.value))}
              className="w-14 bg-studio-950 border border-studio-700 rounded px-2 py-1 text-xs text-white font-mono text-center"
            />
            <button
              onClick={handleGenerateShots}
              disabled={generating}
              className="bg-gradient-to-r from-purple-600 to-indigo-600 hover:opacity-90 text-white px-4 py-1.5 rounded-lg text-xs font-bold flex items-center space-x-1.5 shadow-md shadow-purple-900/20 transition-all disabled:opacity-50"
            >
              <Wand2 className="w-3.5 h-3.5" />
              <span>{generating ? 'Deconstructing...' : 'Director Agent Breakdown'}</span>
            </button>
          </div>
        )}
      </div>

      {/* Continuity Report Modal */}
      {showContinuityModal && continuityReport && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-studio-900 border border-studio-700 rounded-xl max-w-lg w-full p-6 space-y-4 shadow-2xl">
            <div className="flex items-center justify-between border-b border-studio-800 pb-3">
              <div className="flex items-center space-x-2">
                {continuityReport.score >= 80 ? (
                  <ShieldCheck className="w-5 h-5 text-emerald-400" />
                ) : (
                  <ShieldAlert className="w-5 h-5 text-cinema-amber" />
                )}
                <h3 className="text-sm font-bold text-white uppercase tracking-wider">
                  Continuity Supervisor Report
                </h3>
              </div>
              <span
                className={`text-xs font-mono font-bold px-2 py-0.5 rounded ${
                  continuityReport.score >= 80 ? 'bg-emerald-500/20 text-emerald-400' : 'bg-cinema-amber/20 text-cinema-gold'
                }`}
              >
                Score: {continuityReport.score}/100 ({continuityReport.status})
              </span>
            </div>

            <div className="space-y-3 max-h-64 overflow-y-auto">
              {continuityReport.issues.length > 0 && (
                <div className="space-y-1.5">
                  <span className="text-[10px] uppercase font-mono font-bold text-cinema-crimson">
                    Detected Discrepancies:
                  </span>
                  {continuityReport.issues.map((iss, i) => (
                    <div key={i} className="text-xs text-rose-300 bg-rose-950/30 p-2 rounded border border-rose-900/40">
                      • {iss}
                    </div>
                  ))}
                </div>
              )}

              <div className="space-y-1.5">
                <span className="text-[10px] uppercase font-mono font-bold text-cyan-400">
                  Supervisor Suggestions:
                </span>
                {continuityReport.suggestions.map((sug, i) => (
                  <div key={i} className="text-xs text-slate-300 bg-studio-950 p-2 rounded border border-studio-800">
                    • {sug}
                  </div>
                ))}
              </div>
            </div>

            <div className="flex justify-end pt-2 border-t border-studio-800">
              <button
                onClick={() => setShowContinuityModal(false)}
                className="bg-studio-800 hover:bg-studio-700 text-white px-4 py-1.5 rounded-lg text-xs font-semibold"
              >
                Close Report
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Manual Add Shot Modal */}
      {showAddModal && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-studio-900 border border-studio-700 rounded-xl max-w-lg w-full p-6 space-y-4 shadow-2xl">
            <h3 className="text-sm font-bold text-white uppercase tracking-wider">Add Manual Storyboard Shot</h3>
            <form onSubmit={handleAddManualShot} className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Camera Angle</label>
                  <input
                    type="text"
                    value={newAngle}
                    onChange={(e) => setNewAngle(e.target.value)}
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                  />
                </div>
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Camera Movement</label>
                  <input
                    type="text"
                    value={newMovement}
                    onChange={(e) => setNewMovement(e.target.value)}
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Duration (seconds)</label>
                  <input
                    type="number"
                    step={0.5}
                    value={newDuration}
                    onChange={(e) => setNewDuration(Number(e.target.value))}
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white font-mono"
                  />
                </div>
                <div>
                  <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Lighting</label>
                  <input
                    type="text"
                    value={newLighting}
                    onChange={(e) => setNewLighting(e.target.value)}
                    className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                  />
                </div>
              </div>

              <div>
                <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">Visual Description</label>
                <textarea
                  rows={2}
                  required
                  value={newDesc}
                  onChange={(e) => setNewDesc(e.target.value)}
                  placeholder="What happens in front of the lens..."
                  className="w-full bg-studio-950 border border-studio-700 rounded px-2.5 py-1.5 text-xs text-white"
                />
              </div>

              <div>
                <label className="block text-[10px] uppercase font-mono text-slate-400 mb-1">
                  Custom Video Prompt (Optional)
                </label>
                <textarea
                  rows={2}
                  value={newPrompt}
                  onChange={(e) => setNewPrompt(e.target.value)}
                  placeholder="Leave blank to auto-compose from angle and lighting..."
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
                  Save Shot
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Storyboard Cards Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {shots.map((shot) => {
          const isCopied = copiedShotId === shot.id;
          return (
            <div
              key={shot.id}
              className="rounded-xl bg-studio-900 border border-studio-800 overflow-hidden shadow-lg flex flex-col justify-between hover:border-studio-700 transition-all"
            >
              {/* Card Header */}
              <div className="p-4 border-b border-studio-800 bg-studio-950/40 flex items-center justify-between">
                <div className="flex items-center space-x-2">
                  <span className="text-xs font-mono font-bold text-cinema-gold px-2 py-0.5 rounded bg-studio-900 border border-cinema-gold/30">
                    {shot.shot_code}
                  </span>
                  <span className="text-xs font-mono text-slate-400 flex items-center space-x-1">
                    <Clock className="w-3.5 h-3.5 text-slate-500" />
                    <span>{shot.duration_seconds}s</span>
                  </span>
                </div>

                <div className="flex items-center space-x-2">
                  <select
                    value={shot.generation_status}
                    onChange={(e) => handleStatusChange(shot.id, e.target.value)}
                    className={`text-[10px] font-mono font-bold px-2 py-1 rounded border focus:outline-none ${
                      shot.generation_status === 'approved' || shot.generation_status === 'imported'
                        ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/40'
                        : shot.generation_status === 'ready_for_flow'
                        ? 'bg-cyan-500/20 text-cyan-400 border-cyan-500/40'
                        : 'bg-studio-850 text-slate-400 border-studio-700'
                    }`}
                  >
                    <option value="pending">Pending</option>
                    <option value="ready_for_flow">Ready for Flow</option>
                    <option value="generating">Generating</option>
                    <option value="imported">Imported</option>
                    <option value="approved">Approved</option>
                  </select>

                  <button
                    onClick={() => handleDeleteShot(shot.id)}
                    className="text-slate-500 hover:text-cinema-crimson p-1 transition-colors"
                    title="Delete shot"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              </div>

              {/* Card Body */}
              <div className="p-5 space-y-3">
                {/* Angle & Movement Badges */}
                <div className="flex flex-wrap gap-2 text-[10px] font-mono">
                  <span className="bg-studio-950 text-purple-300 px-2 py-0.5 rounded border border-purple-800/30">
                    {shot.camera_angle}
                  </span>
                  <span className="bg-studio-950 text-cyan-300 px-2 py-0.5 rounded border border-cyan-800/30">
                    {shot.camera_movement}
                  </span>
                  <span className="bg-studio-950 text-slate-300 px-2 py-0.5 rounded border border-studio-800">
                    {shot.lighting}
                  </span>
                </div>

                {/* Visual Description */}
                <p className="text-xs text-slate-200 leading-relaxed font-sans">{shot.visual_description}</p>

                {/* Video Prompt for Google Flow */}
                <div className="p-3 rounded-lg bg-studio-950 border border-studio-800/80 space-y-1.5">
                  <div className="flex items-center justify-between">
                    <span className="text-[9px] uppercase font-mono font-bold text-cyan-400 tracking-wider">
                      Google Flow Prompt
                    </span>
                    <button
                      onClick={() => handleCopyPrompt(shot)}
                      className="text-[10px] text-cinema-gold hover:text-white flex items-center space-x-1 font-mono transition-colors"
                    >
                      {isCopied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
                      <span>{isCopied ? 'Copied!' : 'Copy Prompt'}</span>
                    </button>
                  </div>
                  <p className="text-[11px] text-slate-300 font-mono line-clamp-3 leading-snug">
                    {shot.video_prompt}
                  </p>
                </div>

                {/* Media linked indicator if imported */}
                {shot.media_url && (
                  <div className="p-2 rounded bg-emerald-500/10 border border-emerald-500/30 flex items-center space-x-2 text-xs text-emerald-400 font-mono">
                    <Video className="w-4 h-4 shrink-0" />
                    <span className="truncate">Footage linked: {shot.media_url.split('/').pop()}</span>
                  </div>
                )}
              </div>

              {/* Card Footer Actions */}
              <div className="px-5 py-3 bg-studio-950/60 border-t border-studio-800 flex items-center justify-between text-xs">
                <button
                  onClick={() => handleOptimizePrompt(shot.id)}
                  className="text-cyan-400 hover:text-cyan-300 font-medium flex items-center space-x-1 transition-colors"
                >
                  <Sparkles className="w-3.5 h-3.5" />
                  <span>Cinematographer Polish</span>
                </button>

                <button
                  onClick={() => handleCopyPrompt(shot)}
                  className="bg-studio-800 hover:bg-studio-700 text-slate-200 px-3 py-1 rounded text-[11px] font-semibold border border-studio-700 transition-colors"
                >
                  {isCopied ? 'Copied to Clipboard' : 'Copy Flow Prompt'}
                </button>
              </div>
            </div>
          );
        })}

        {shots.length === 0 && !loading && (
          <div className="col-span-2 p-12 text-center text-slate-500 text-xs font-mono space-y-3">
            <Layers className="w-8 h-8 text-slate-600 mx-auto" />
            <p>No shots created for this scene yet.</p>
            <p className="text-slate-400">
              Click <strong className="text-cinema-gold">"Director Agent Breakdown"</strong> above to automatically decompose the scene into Flow shots!
            </p>
          </div>
        )}
      </div>
    </div>
  );
};
