import React, { useState, useEffect } from 'react';
import {
  Project,
  Scene,
  fetchScenes,
  createScene,
  generateScene,
  updateScene,
  deleteScene,
  refineScene,
  fetchStoryReview,
  submitStoryForReview,
  decideStoryApproval,
  fetchCheckpointDetail,
  PipelineCheckpointDetail,
  StoryReviewData,
} from '../api/client';
import {
  BookOpen,
  Plus,
  Sparkles,
  Trash2,
  Save,
  Wand2,
  Layers,
  Clock,
  Coins,
  FileText,
  AlignLeft,
  ShieldCheck,
  ShieldAlert,
  CheckCircle2,
  AlertTriangle,
  Send,
  Eye,
  X
} from 'lucide-react';

import { ViewTab } from '../components/Sidebar';

interface ScreenplayViewProps {
  activeProject: Project | null;
  onSelectView: (view: ViewTab) => void;
  onRefreshProject: () => void;
}

export const ScreenplayView: React.FC<ScreenplayViewProps> = ({
  activeProject,
  onSelectView,
  onRefreshProject,
}) => {
  const [scenes, setScenes] = useState<Scene[]>([]);
  const [selectedScene, setSelectedScene] = useState<Scene | null>(null);
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [refining, setRefining] = useState(false);
  const [saving, setSaving] = useState(false);

  // Form states for active scene
  const [slugline, setSlugline] = useState('');
  const [title, setTitle] = useState('');
  const [summary, setSummary] = useState('');
  const [scriptContent, setScriptContent] = useState('');
  const [duration, setDuration] = useState(15);
  const [budgetLimit, setBudgetLimit] = useState(20.0);

  // AI prompt state
  const [aiPrompt, setAiPrompt] = useState('');
  const [refineDirection, setRefineDirection] = useState('Intensify dramatic tension and subtext');
  const [viewMode, setViewMode] = useState<'formatted' | 'edit'>('edit');

  // Story Approval state
  const [checkpoint, setCheckpoint] = useState<PipelineCheckpointDetail | null>(null);
  const [checkpointLoading, setCheckpointLoading] = useState(false);
  const [submittingApproval, setSubmittingApproval] = useState(false);
  const [reviewModalOpen, setReviewModalOpen] = useState(false);
  const [reviewData, setReviewData] = useState<StoryReviewData | null>(null);
  const [loadingReviewData, setLoadingReviewData] = useState(false);
  const [reviewerName, setReviewerName] = useState('Director');
  const [feedbackNotes, setFeedbackNotes] = useState('');
  const [decisionSubmitting, setDecisionSubmitting] = useState(false);
  const [decisionError, setDecisionError] = useState<string | null>(null);

  const loadCheckpoint = async () => {
    if (!activeProject) return;
    setCheckpointLoading(true);
    try {
      const data = await fetchCheckpointDetail(activeProject.id, 'story_approval');
      setCheckpoint(data);
    } catch (err) {
      console.error('Failed to load checkpoint:', err);
    } finally {
      setCheckpointLoading(false);
    }
  };

  const handleSubmitForReview = async () => {
    if (!activeProject) return;
    setSubmittingApproval(true);
    try {
      await submitStoryForReview(activeProject.id);
      await loadCheckpoint();
      onRefreshProject();
    } catch (err: any) {
      alert('Submit for review failed: ' + (err.message || err));
    } finally {
      setSubmittingApproval(false);
    }
  };

  const handleOpenReviewModal = async () => {
    if (!activeProject) return;
    setReviewModalOpen(true);
    setLoadingReviewData(true);
    setDecisionError(null);
    try {
      const data = await fetchStoryReview(activeProject.id);
      setReviewData(data);
    } catch (err: any) {
      setDecisionError('Failed to load review data: ' + (err.message || err));
    } finally {
      setLoadingReviewData(false);
    }
  };

  const handleDecision = async (decision: 'APPROVED' | 'REJECTED' | 'REQUEST_CHANGES') => {
    if (!activeProject || !reviewData) return;
    setDecisionSubmitting(true);
    setDecisionError(null);
    try {
      await decideStoryApproval(activeProject.id, {
        decision,
        expected_artifact_version: reviewData.artifact_version,
        expected_artifact_hash: reviewData.current_artifact_hash || '',
        reviewer_name: reviewerName || 'Director',
        feedback_notes: feedbackNotes || undefined,
      });
      setReviewModalOpen(false);
      setFeedbackNotes('');
      await loadCheckpoint();
      onRefreshProject();
    } catch (err: any) {
      if (err.status === 409) {
        setDecisionError('Conflict (409): Screenplay was modified while reviewing. The approval state has been marked STALE. Refreshing review draft...');
        try {
          const fresh = await fetchStoryReview(activeProject.id);
          setReviewData(fresh);
          await loadCheckpoint();
        } catch (_) {}
      } else {
        setDecisionError('Decision failed: ' + (err.message || err));
      }
    } finally {
      setDecisionSubmitting(false);
    }
  };


  const loadScenes = async () => {
    if (!activeProject) return;
    setLoading(true);
    try {
      const data = await fetchScenes(activeProject.id);
      setScenes(data);
      if (data.length > 0) {
        setSelectedScene((prev) => {
          if (prev) {
            const found = data.find((s) => s.id === prev.id);
            if (found) return found;
          }
          return data[0];
        });
      } else {
        setSelectedScene(null);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadScenes();
    loadCheckpoint();
  }, [activeProject?.id]);

  useEffect(() => {
    if (selectedScene) {
      setSlugline(selectedScene.slugline);
      setTitle(selectedScene.title);
      setSummary(selectedScene.summary);
      setScriptContent(selectedScene.script_content);
      setDuration(selectedScene.estimated_duration);
      setBudgetLimit(selectedScene.budget_limit);
    }
  }, [selectedScene?.id]);

  const handleSaveScene = async () => {
    if (!selectedScene) return;
    setSaving(true);
    try {
      const updated = await updateScene(selectedScene.id, {
        slugline,
        title,
        summary,
        script_content: scriptContent,
        estimated_duration: duration,
        budget_limit: budgetLimit,
      });
      setSelectedScene(updated);
      await loadScenes();
      await loadCheckpoint();
      onRefreshProject();
    } catch (err) {
      alert('Failed to save scene: ' + err);
    } finally {
      setSaving(false);
    }
  };

  const handleCreateEmptyScene = async () => {
    if (!activeProject) return;
    try {
      const nextNum = scenes.length + 1;
      const created = await createScene({
        project_id: activeProject.id,
        scene_number: nextNum,
        slugline: `INT. UNTITLED SET ${nextNum} - NIGHT`,
        title: `Scene ${nextNum}`,
        summary: '',
        script_content: `INT. UNTITLED SET ${nextNum} - NIGHT\n\nAction description goes here...\n`,
        estimated_duration: 15,
        budget_limit: 20.0,
      });
      await loadScenes();
      await loadCheckpoint();
      setSelectedScene(created);
      onRefreshProject();
    } catch (err) {
      alert('Failed to create scene: ' + err);
    }
  };

  const handleGenerateAiScene = async () => {
    if (!activeProject) return;
    setGenerating(true);
    try {
      const created = await generateScene(activeProject.id, aiPrompt);
      setAiPrompt('');
      await loadScenes();
      await loadCheckpoint();
      setSelectedScene(created);
      onRefreshProject();
    } catch (err: any) {
      alert('Scene generation error: ' + (err.message || err));
    } finally {
      setGenerating(false);
    }
  };

  const handleRefineDialogue = async () => {
    if (!selectedScene) return;
    setRefining(true);
    try {
      const res = await refineScene(selectedScene.id, refineDirection);
      setScriptContent(res.refined_script);
      await handleSaveScene();
      await loadCheckpoint();
    } catch (err: any) {
      alert('Refinement error: ' + (err.message || err));
    } finally {
      setRefining(false);
    }
  };

  const handleDeleteScene = async () => {
    if (!selectedScene) return;
    if (!window.confirm(`Delete "${selectedScene.slugline}" and its shots?`)) return;
    try {
      await deleteScene(selectedScene.id);
      await loadScenes();
      await loadCheckpoint();
      onRefreshProject();
    } catch (err) {
      alert('Delete failed: ' + err);
    }
  };


  if (!activeProject) {
    return <div className="p-8 text-center text-slate-400">Please select a project first.</div>;
  }

  return (
    <div className="flex h-full overflow-hidden">
      {/* Left: Scenes List Sidebar */}
      <div className="w-72 bg-studio-900 border-r border-studio-800 flex flex-col h-full">
        <div className="p-4 border-b border-studio-800 flex items-center justify-between bg-studio-950/40">
          <div>
            <h3 className="text-xs font-bold uppercase tracking-wider text-white">Scene List</h3>
            <span className="text-[10px] font-mono text-slate-400">{scenes.length} scenes written</span>
          </div>
          <button
            onClick={handleCreateEmptyScene}
            className="bg-studio-800 hover:bg-studio-700 text-cinema-gold p-1.5 rounded-lg border border-studio-700 transition-colors"
            title="Add blank scene"
          >
            <Plus className="w-4 h-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-2 space-y-1.5">
          {scenes.map((s) => {
            const isSelected = selectedScene?.id === s.id;
            return (
              <button
                key={s.id}
                onClick={() => setSelectedScene(s)}
                className={`w-full text-left p-3 rounded-lg border transition-all ${
                  isSelected
                    ? 'bg-studio-800 border-cinema-gold/60 text-white shadow-sm'
                    : 'bg-studio-950/40 border-studio-800/80 hover:border-studio-700 text-slate-300'
                }`}
              >
                <div className="flex items-center justify-between mb-1">
                  <span className="text-[10px] font-mono font-bold text-cinema-gold">
                    SCENE {s.scene_number}
                  </span>
                  <span className="text-[9px] font-mono text-slate-400 bg-studio-900 px-1.5 py-0.5 rounded border border-studio-800">
                    {s.estimated_duration}s
                  </span>
                </div>
                <h4 className="text-xs font-semibold truncate leading-tight">{s.slugline}</h4>
                <div className="flex items-center justify-between text-[10px] text-slate-500 mt-2 font-mono">
                  <span>{s.shots_count || 0} shots</span>
                  <span>Budget: ${s.budget_limit}</span>
                </div>
              </button>
            );
          })}
          {scenes.length === 0 && !loading && (
            <div className="p-6 text-center text-xs text-slate-500">
              No scenes created. Use the AI Screenwriter panel or click + to add a scene.
            </div>
          )}
        </div>
      </div>

      {/* Middle: Screenplay Editor */}
      <div className="flex-1 flex flex-col h-full bg-studio-950 overflow-hidden">
        {/* Story Approval Lifecycle Bar */}
        <div className="px-4 py-2.5 bg-studio-900 border-b border-studio-800 flex items-center justify-between gap-4">
          <div className="flex items-center space-x-3">
            <div className="flex items-center space-x-1.5">
              <span className="text-[11px] font-mono uppercase tracking-wider text-slate-400 font-semibold">
                Story Approval:
              </span>
              <span className={`text-[10px] font-mono px-2 py-0.5 rounded-full border font-bold ${
                checkpoint?.status === 'APPROVED'
                  ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                  : checkpoint?.status === 'WAITING_FOR_APPROVAL'
                  ? 'bg-amber-500/10 text-amber-300 border-amber-500/40 animate-pulse'
                  : checkpoint?.status === 'STALE'
                  ? 'bg-amber-500/10 text-amber-400 border-amber-500/30'
                  : checkpoint?.status === 'REJECTED'
                  ? 'bg-rose-500/10 text-rose-400 border-rose-500/30'
                  : 'bg-slate-800 text-slate-400 border-slate-700'
              }`}>
                {checkpoint?.status || 'LOCKED'}
              </span>
            </div>

            {checkpoint && (
              <div className="flex items-center space-x-2 text-[10px] font-mono text-slate-400">
                <span>v{checkpoint.artifact_version}</span>
                <span>•</span>
                <span title={checkpoint.artifact_content_hash || undefined}>
                  Hash: {checkpoint.artifact_content_hash ? checkpoint.artifact_content_hash.slice(0, 8) + '...' : 'none'}
                </span>
                {checkpoint.status === 'APPROVED' && (
                  <>
                    <span>•</span>
                    <span className="text-emerald-400 flex items-center space-x-1">
                      <CheckCircle2 className="w-3 h-3 inline" />
                      <span>Approved by {checkpoint.reviewer_name}</span>
                    </span>
                  </>
                )}
                {checkpoint.status === 'STALE' && (
                  <>
                    <span>•</span>
                    <span className="text-amber-400 flex items-center space-x-1">
                      <AlertTriangle className="w-3 h-3 inline" />
                      <span>Script modified — re-approval required</span>
                    </span>
                  </>
                )}
              </div>
            )}
          </div>

          <div className="flex items-center space-x-2">
            {checkpoint?.status === 'WAITING_FOR_APPROVAL' ? (
              <button
                onClick={handleOpenReviewModal}
                className="bg-amber-500 hover:bg-amber-400 text-studio-950 font-bold px-3 py-1 rounded text-xs flex items-center space-x-1 shadow transition-all active:scale-95"
              >
                <Eye className="w-3.5 h-3.5" />
                <span>Review & Decide</span>
              </button>
            ) : checkpoint?.status === 'APPROVED' ? (
              <button
                onClick={() => onSelectView('storyboard')}
                className="bg-emerald-600 hover:bg-emerald-500 text-white font-bold px-3 py-1 rounded text-xs flex items-center space-x-1 shadow transition-all active:scale-95"
              >
                <Layers className="w-3.5 h-3.5" />
                <span>Storyboard Shots →</span>
              </button>
            ) : (
              <button
                onClick={handleSubmitForReview}
                disabled={submittingApproval || scenes.length === 0}
                className="bg-studio-800 hover:bg-studio-700 text-cinema-gold border border-cinema-gold/40 font-semibold px-3 py-1 rounded text-xs flex items-center space-x-1 transition-all disabled:opacity-50"
                title={scenes.length === 0 ? "Add at least one scene to submit" : "Submit screenplay draft for human approval"}
              >
                <Send className="w-3.5 h-3.5" />
                <span>{submittingApproval ? 'Submitting...' : checkpoint?.status === 'STALE' ? 'Re-Submit for Review' : 'Submit for Review'}</span>
              </button>
            )}
          </div>
        </div>

        {selectedScene ? (

          <>
            {/* Scene Header & Metadata */}
            <div className="p-4 border-b border-studio-800 bg-studio-900/60 flex items-center justify-between">
              <div className="space-y-1 flex-1 max-w-xl">
                <input
                  type="text"
                  value={slugline}
                  onChange={(e) => setSlugline(e.target.value)}
                  className="w-full bg-transparent font-mono font-bold text-sm text-cinema-gold uppercase focus:outline-none focus:bg-studio-950/80 px-2 py-0.5 rounded border border-transparent focus:border-studio-700"
                  placeholder="INT. SET - TIME"
                />
                <input
                  type="text"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  className="w-full bg-transparent text-xs text-slate-300 focus:outline-none focus:bg-studio-950/80 px-2 py-0.5 rounded border border-transparent focus:border-studio-700"
                  placeholder="Scene Title or Dramatic Premise"
                />
              </div>

              {/* Action Buttons */}
              <div className="flex items-center space-x-2">
                <div className="flex items-center bg-studio-950 border border-studio-800 rounded-lg p-0.5">
                  <button
                    onClick={() => setViewMode('edit')}
                    className={`px-2.5 py-1 rounded text-xs font-mono font-medium ${
                      viewMode === 'edit' ? 'bg-studio-800 text-white' : 'text-slate-400 hover:text-white'
                    }`}
                  >
                    Editor
                  </button>
                  <button
                    onClick={() => setViewMode('formatted')}
                    className={`px-2.5 py-1 rounded text-xs font-mono font-medium ${
                      viewMode === 'formatted' ? 'bg-studio-800 text-white' : 'text-slate-400 hover:text-white'
                    }`}
                  >
                    Fountain View
                  </button>
                </div>

                <button
                  onClick={handleSaveScene}
                  disabled={saving}
                  className="bg-cinema-gold hover:bg-cinema-amber text-studio-950 px-3.5 py-1.5 rounded-lg text-xs font-bold flex items-center space-x-1.5 shadow transition-all active:scale-95"
                >
                  <Save className="w-3.5 h-3.5" />
                  <span>{saving ? 'Saving...' : 'Save'}</span>
                </button>

                <button
                  onClick={handleDeleteScene}
                  className="text-slate-500 hover:text-cinema-crimson p-1.5 rounded hover:bg-studio-900 transition-colors"
                  title="Delete scene"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            </div>

            {/* Script Text Area or Fountain Formatter */}
            <div className="flex-1 overflow-y-auto p-6">
              {viewMode === 'edit' ? (
                <textarea
                  value={scriptContent}
                  onChange={(e) => setScriptContent(e.target.value)}
                  placeholder="Write action lines, character names in caps, parentheticals, and dialogue..."
                  className="w-full h-full bg-studio-900/50 border border-studio-800 rounded-xl p-6 text-slate-100 font-mono text-sm leading-relaxed focus:outline-none focus:border-cinema-gold/40 resize-none shadow-inner"
                  spellCheck={false}
                />
              ) : (
                <div className="max-w-2xl mx-auto bg-studio-900 border border-studio-800 rounded-xl p-8 font-mono text-xs text-slate-200 shadow-xl space-y-4">
                  <div className="text-center font-bold text-cinema-gold tracking-widest text-sm border-b border-studio-800 pb-3">
                    {slugline}
                  </div>
                  <div className="whitespace-pre-wrap leading-relaxed">
                    {scriptContent.split('\n\n').map((block, i) => {
                      const isDialogue = block.includes('\n') && block.split('\n')[0] === block.split('\n')[0].toUpperCase();
                      return (
                        <div key={i} className={`my-3 ${isDialogue ? 'pl-16 pr-8 text-cyan-200' : 'text-slate-300'}`}>
                          {block}
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>

            {/* Bottom Scene Quick Stats */}
            <div className="px-6 py-2.5 bg-studio-900 border-t border-studio-800 flex items-center justify-between text-xs text-slate-400 font-mono">
              <div className="flex items-center space-x-4">
                <span className="flex items-center space-x-1">
                  <Clock className="w-3.5 h-3.5 text-cinema-gold" />
                  <span>Est. Duration: {duration}s</span>
                </span>
                <span className="flex items-center space-x-1">
                  <Coins className="w-3.5 h-3.5 text-emerald-400" />
                  <span>Budget Limit: ${budgetLimit}</span>
                </span>
              </div>

              <button
                onClick={() => onSelectView('storyboard')}
                className="text-cinema-gold hover:underline flex items-center space-x-1"
              >
                <Layers className="w-3.5 h-3.5" />
                <span>Jump to Storyboard Shots →</span>
              </button>
            </div>
          </>
        ) : (
          <div className="h-full flex items-center justify-center text-slate-500 text-xs font-mono">
            Select a scene from the left or generate a new one with the AI Screenwriter.
          </div>
        )}
      </div>

      {/* Right: AI Screenwriter & Director Agent Panel */}
      <div className="w-80 bg-studio-900 border-l border-studio-800 flex flex-col h-full overflow-y-auto p-4 space-y-5">
        {/* Screenwriter Agent Box */}
        <div className="p-4 rounded-xl bg-studio-950 border border-cinema-amber/30 space-y-3 shadow-lg">
          <div className="flex items-center space-x-2 text-cinema-gold">
            <Sparkles className="w-4 h-4 text-cinema-gold" />
            <h3 className="text-xs font-bold uppercase tracking-wider">AI Screenwriter Agent</h3>
          </div>
          <p className="text-[11px] text-slate-400">
            Generates high-tension cinematic scenes adhering to character consistency and project lore.
          </p>

          <textarea
            rows={3}
            value={aiPrompt}
            onChange={(e) => setAiPrompt(e.target.value)}
            placeholder="e.g. Kai breaches the sub-level chamber and discovers an anomaly in the cooling conduits..."
            className="w-full bg-studio-900 border border-studio-700 rounded-lg p-2.5 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-cinema-gold font-sans"
          />

          <button
            onClick={handleGenerateAiScene}
            disabled={generating}
            className="w-full bg-gradient-to-r from-cinema-amber to-cinema-gold hover:opacity-90 text-studio-950 font-bold py-2 rounded-lg text-xs flex items-center justify-center space-x-1.5 shadow transition-all disabled:opacity-50"
          >
            <Wand2 className="w-3.5 h-3.5 stroke-[2.5]" />
            <span>{generating ? 'Drafting Scene...' : 'Generate Scene with AI'}</span>
          </button>
        </div>

        {/* Refinement Panel */}
        {selectedScene && (
          <div className="p-4 rounded-xl bg-studio-950 border border-studio-800 space-y-3">
            <div className="flex items-center space-x-2 text-cyan-400">
              <FileText className="w-4 h-4" />
              <h3 className="text-xs font-bold uppercase tracking-wider">Dialogue & Beat Tuning</h3>
            </div>
            <p className="text-[11px] text-slate-400">
              Instruct the agent to punch up subtext, adjust conflict pacing, or polish lines.
            </p>

            <input
              type="text"
              value={refineDirection}
              onChange={(e) => setRefineDirection(e.target.value)}
              placeholder="e.g. Make dialogue sharper and more secretive"
              className="w-full bg-studio-900 border border-studio-700 rounded-lg px-2.5 py-1.5 text-xs text-white focus:outline-none focus:border-cyan-400"
            />

            <button
              onClick={handleRefineDialogue}
              disabled={refining}
              className="w-full bg-studio-800 hover:bg-studio-700 text-cyan-300 border border-cyan-800/40 font-semibold py-1.5 rounded-lg text-xs flex items-center justify-center space-x-1.5 transition-all disabled:opacity-50"
            >
              <Sparkles className="w-3.5 h-3.5 text-cyan-400" />
              <span>{refining ? 'Polishing...' : 'Refine Scene Text'}</span>
            </button>
          </div>
        )}

        {/* Director Agent Breakdown Card */}
        {selectedScene && (
          <div className="p-4 rounded-xl bg-studio-950 border border-studio-800 space-y-3">
            <div className="flex items-center space-x-2 text-purple-400">
              <Layers className="w-4 h-4" />
              <h3 className="text-xs font-bold uppercase tracking-wider">Director Agent</h3>
            </div>
            <p className="text-[11px] text-slate-400">
              Ready to deconstruct this scene into shot angles, camera movements, and Google Flow prompts?
            </p>

            <button
              onClick={() => onSelectView('storyboard')}
              className="w-full bg-studio-800 hover:bg-studio-700 text-purple-300 border border-purple-800/40 font-semibold py-2 rounded-lg text-xs flex items-center justify-center space-x-1.5 transition-all"
            >
              <Layers className="w-3.5 h-3.5 text-purple-400" />
              <span>Breakdown into Shots →</span>
            </button>
          </div>
        )}
      </div>

      {/* Director Story Approval Review Modal */}
      {reviewModalOpen && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="bg-studio-900 border border-studio-700 rounded-xl max-w-2xl w-full p-6 space-y-4 shadow-2xl flex flex-col max-h-[90vh]">
            <div className="flex items-center justify-between border-b border-studio-800 pb-3">
              <div className="flex items-center space-x-2">
                <ShieldCheck className="w-5 h-5 text-cinema-gold" />
                <h3 className="text-sm font-bold text-white uppercase tracking-wider font-mono">
                  Director Story Approval Review
                </h3>
              </div>
              <button
                onClick={() => setReviewModalOpen(false)}
                className="text-slate-400 hover:text-white p-1 rounded"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {loadingReviewData ? (
              <div className="p-8 text-center text-slate-400 text-xs font-mono">
                Loading canonical review draft...
              </div>
            ) : reviewData ? (
              <div className="flex-1 overflow-y-auto space-y-4 pr-1">
                {/* Concurrency Token Box */}
                <div className="p-3 rounded-lg bg-studio-950 border border-studio-800 space-y-1">
                  <div className="flex items-center justify-between text-xs font-mono">
                    <span className="text-slate-400">Canonical Artifact Version:</span>
                    <span className="text-cinema-gold font-bold">v{reviewData.artifact_version}</span>
                  </div>
                  <div className="flex items-center justify-between text-xs font-mono">
                    <span className="text-slate-400">Canonical SHA-256 Hash:</span>
                    <span className="text-slate-300 font-mono text-[10px] truncate max-w-xs" title={reviewData.current_artifact_hash || ''}>
                      {reviewData.current_artifact_hash}
                    </span>
                  </div>
                </div>

                {/* Error Banner */}
                {decisionError && (
                  <div className="p-3 rounded-lg bg-rose-950/80 border border-rose-700 text-rose-200 text-xs space-y-1">
                    <div className="font-bold flex items-center space-x-1.5">
                      <AlertTriangle className="w-4 h-4 text-rose-400 inline" />
                      <span>Concurrency Collision / Error:</span>
                    </div>
                    <p className="text-[11px] leading-relaxed">{decisionError}</p>
                  </div>
                )}

                {/* Scenes Preview */}
                <div className="space-y-2">
                  <h4 className="text-xs font-bold uppercase tracking-wider text-slate-300 font-mono">
                    Screenplay Scenes Draft ({reviewData.scenes.length} Scenes)
                  </h4>
                  <div className="space-y-2 max-h-56 overflow-y-auto pr-1">
                    {reviewData.scenes.map((sc) => (
                      <div key={sc.scene_number} className="p-3 rounded bg-studio-950 border border-studio-800 space-y-1">
                        <div className="flex items-center justify-between">
                          <span className="text-[11px] font-mono font-bold text-cinema-gold">
                            SCENE {sc.scene_number}: {sc.slugline}
                          </span>
                          <span className="text-[10px] font-mono text-slate-500">{sc.estimated_duration}s</span>
                        </div>
                        {sc.summary && (
                          <p className="text-[11px] text-slate-400 italic">{sc.summary}</p>
                        )}
                        <pre className="text-[11px] font-mono text-slate-300 whitespace-pre-wrap bg-studio-900/60 p-2 rounded border border-studio-800/60 max-h-24 overflow-y-auto">
                          {sc.script_content}
                        </pre>
                      </div>
                    ))}
                  </div>
                </div>

                {/* Reviewer inputs */}
                <div className="space-y-3 pt-2 border-t border-studio-800">
                  <div>
                    <label className="block text-[11px] font-mono uppercase text-slate-400 mb-1">
                      Reviewer Name / Title:
                    </label>
                    <input
                      type="text"
                      value={reviewerName}
                      onChange={(e) => setReviewerName(e.target.value)}
                      placeholder="e.g. Lead Director, Executive Producer"
                      className="w-full bg-studio-950 border border-studio-700 rounded-lg px-3 py-1.5 text-xs text-white focus:outline-none focus:border-cinema-gold"
                    />
                  </div>

                  <div>
                    <label className="block text-[11px] font-mono uppercase text-slate-400 mb-1">
                      Feedback / Revision Notes:
                    </label>
                    <textarea
                      rows={2}
                      value={feedbackNotes}
                      onChange={(e) => setFeedbackNotes(e.target.value)}
                      placeholder="Optional notes or revision instructions for the writing room..."
                      className="w-full bg-studio-950 border border-studio-700 rounded-lg p-2 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-cinema-gold"
                    />
                  </div>
                </div>

                {/* Decision Actions */}
                <div className="pt-2 flex items-center justify-end space-x-2 border-t border-studio-800">
                  <button
                    onClick={() => handleDecision('REJECTED')}
                    disabled={decisionSubmitting}
                    className="bg-rose-900/80 hover:bg-rose-800 text-rose-200 border border-rose-700 font-semibold px-3 py-1.5 rounded-lg text-xs transition-all disabled:opacity-50"
                  >
                    Reject
                  </button>
                  <button
                    onClick={() => handleDecision('REQUEST_CHANGES')}
                    disabled={decisionSubmitting}
                    className="bg-amber-900/80 hover:bg-amber-800 text-amber-200 border border-amber-700 font-semibold px-3 py-1.5 rounded-lg text-xs transition-all disabled:opacity-50"
                  >
                    Request Changes
                  </button>
                  <button
                    onClick={() => handleDecision('APPROVED')}
                    disabled={decisionSubmitting}
                    className="bg-emerald-600 hover:bg-emerald-500 text-white font-bold px-4 py-1.5 rounded-lg text-xs flex items-center space-x-1.5 shadow transition-all disabled:opacity-50"
                  >
                    <CheckCircle2 className="w-3.5 h-3.5" />
                    <span>{decisionSubmitting ? 'Recording...' : 'Approve Screenplay'}</span>
                  </button>
                </div>
              </div>
            ) : null}
          </div>
        </div>
      )}
    </div>
  );

};
