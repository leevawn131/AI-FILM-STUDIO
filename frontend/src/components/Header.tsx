import React, { useState, useEffect } from 'react';
import { Project, fetchAIStatus, updateAIConfig, AIStatusResponse } from '../api/client';
import { Film, DollarSign, Plus, Cpu, ChevronDown, CheckCircle2, AlertCircle } from 'lucide-react';

interface HeaderProps {
  activeProject: Project | null;
  onOpenNewProjectModal: () => void;
  onSelectView: (view: any) => void;
}

export const Header: React.FC<HeaderProps> = ({
  activeProject,
  onOpenNewProjectModal,
  onSelectView,
}) => {
  const [aiStatus, setAiStatus] = useState<AIStatusResponse | null>(null);
  const [showAiMenu, setShowAiMenu] = useState(false);
  const [apiKeyInput, setApiKeyInput] = useState('');
  const [showKeyForm, setShowKeyForm] = useState(false);
  const [isSavingKey, setIsSavingKey] = useState(false);

  useEffect(() => {
    const loadAi = async () => {
      try {
        const s = await fetchAIStatus();
        setAiStatus(s);
      } catch (e) {
        console.error('Failed to load AI status:', e);
      }
    };
    loadAi();
    const interval = setInterval(loadAi, 15000);
    return () => clearInterval(interval);
  }, []);

  const handleSwitchProvider = async (provider: string) => {
    try {
      await updateAIConfig({ provider });
      const s = await fetchAIStatus();
      setAiStatus(s);
      setShowAiMenu(false);
    } catch (e) {
      console.error('Failed to switch AI provider:', e);
    }
  };

  const handleSaveApiKey = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!apiKeyInput.trim()) return;
    setIsSavingKey(true);
    try {
      await updateAIConfig({ gemini_api_key: apiKeyInput.trim(), provider: 'gemini' });
      const s = await fetchAIStatus();
      setAiStatus(s);
      setApiKeyInput('');
      setShowKeyForm(false);
    } catch (e) {
      console.error('Failed to save API key:', e);
    } finally {
      setIsSavingKey(false);
    }
  };

  const budgetTotal = activeProject?.budget_total || 100;
  const budgetSpent = activeProject?.budget_spent || 0;
  const budgetRemaining = Math.max(0, budgetTotal - budgetSpent);
  const percentSpent = Math.min(100, Math.round((budgetSpent / budgetTotal) * 100));

  const isGeminiReady = aiStatus?.gemini?.status === 'configured';
  const isOllamaOnline = aiStatus?.ollama?.status === 'online';
  const isOllamaEnabled = aiStatus?.ollama?.enabled;

  return (
    <header className="h-16 bg-studio-900 border-b border-studio-800 px-6 flex items-center justify-between relative z-40">
      {/* Left: Active Project Info */}
      <div className="flex items-center space-x-4">
        {activeProject ? (
          <div>
            <div className="flex items-center space-x-2.5">
              <h1 className="text-base font-bold text-white tracking-tight">{activeProject.name}</h1>
              <span className="text-[10px] bg-studio-800 text-cinema-cyan px-2 py-0.5 rounded border border-cinema-cyan/20 font-mono font-medium">
                {activeProject.genre}
              </span>
              <span className="text-[10px] bg-studio-800 text-slate-300 px-2 py-0.5 rounded border border-studio-700 font-mono">
                {activeProject.aspect_ratio} Anamorphic
              </span>
              <span className="text-[10px] bg-studio-800 text-slate-300 px-2 py-0.5 rounded border border-studio-700 font-mono">
                {activeProject.fps} FPS
              </span>
            </div>
            <p className="text-xs text-slate-400 truncate max-w-xl mt-0.5 font-normal">
              {activeProject.logline || 'No logline defined yet.'}
            </p>
          </div>
        ) : (
          <div className="text-sm text-slate-400 flex items-center space-x-2">
            <Film className="w-4 h-4 text-slate-500" />
            <span>No project selected. Create or select a project to begin.</span>
          </div>
        )}
      </div>

      {/* Right: AI Provider Pill, Budget Widget & Actions */}
      <div className="flex items-center space-x-3">
        {/* Google Cloud AI Status Pill */}
        <div className="relative">
          <button
            onClick={() => setShowAiMenu(!showAiMenu)}
            className="flex items-center space-x-2 bg-studio-950 border border-studio-800 hover:border-studio-700 px-3 py-1.5 rounded-lg text-xs font-mono transition-all text-slate-300"
            title="Google Cloud AI & Provider Status"
          >
            <Cpu className="w-3.5 h-3.5 text-cinema-cyan" />
            <span className="capitalize">
              {aiStatus?.active_provider === 'gemini'
                ? `Gemini (${aiStatus?.gemini?.configured_model || 'gemini-4-hydrogen'})`
                : aiStatus?.active_provider === 'ollama'
                ? `Ollama (${aiStatus?.ollama?.configured_model || 'local'})`
                : 'Offline Engine'}
            </span>
            <div
              className={`w-2 h-2 rounded-full ${
                aiStatus?.active_provider === 'gemini'
                  ? isGeminiReady
                    ? 'bg-emerald-400 animate-pulse'
                    : 'bg-amber-400'
                  : aiStatus?.active_provider === 'ollama'
                  ? isOllamaOnline
                    ? 'bg-emerald-400'
                    : 'bg-red-400'
                  : 'bg-cinema-cyan'
              }`}
            />
            <ChevronDown className="w-3 h-3 text-slate-500" />
          </button>

          {showAiMenu && (
            <div className="absolute right-0 mt-2 w-80 bg-studio-900 border border-studio-700 rounded-xl shadow-2xl p-3 z-50 text-xs">
              <div className="font-semibold text-slate-200 mb-2 border-b border-studio-800 pb-1.5 flex items-center justify-between">
                <span>AI Intelligence Engine</span>
                <span className="text-[10px] font-mono text-cinema-cyan">Google Cloud AI First</span>
              </div>

              {/* Google Gemini Option (Primary) */}
              <div
                onClick={() => handleSwitchProvider('gemini')}
                className={`p-2.5 rounded-lg cursor-pointer mb-2 transition-all border ${
                  aiStatus?.active_provider === 'gemini'
                    ? 'bg-studio-800 border-cinema-cyan/40 shadow-sm'
                    : 'bg-studio-950/40 border-studio-800 hover:bg-studio-800'
                }`}
              >
                <div className="flex items-start justify-between">
                  <div>
                    <div className="font-semibold text-white flex items-center space-x-1.5">
                      <span>Google Gemini (Primary Cloud AI)</span>
                      {isGeminiReady ? (
                        <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
                      ) : (
                        <AlertCircle className="w-3.5 h-3.5 text-amber-400" />
                      )}
                    </div>
                    <p className="text-[11px] text-slate-400 mt-0.5">
                      {isGeminiReady
                        ? `Connected (${aiStatus?.gemini?.configured_model})`
                        : 'API Key needed for cloud intelligence'}
                    </p>
                  </div>
                </div>

                {!isGeminiReady && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      setShowKeyForm(!showKeyForm);
                    }}
                    className="mt-2 text-[10px] text-cinema-cyan hover:underline font-mono"
                  >
                    {showKeyForm ? 'Hide API Key Form' : '🔑 Enter Gemini API Key'}
                  </button>
                )}

                {showKeyForm && (
                  <form onSubmit={handleSaveApiKey} onClick={(e) => e.stopPropagation()} className="mt-2 pt-2 border-t border-studio-700">
                    <input
                      type="password"
                      value={apiKeyInput}
                      onChange={(e) => setApiKeyInput(e.target.value)}
                      placeholder="Paste Gemini API Key..."
                      className="w-full bg-studio-950 border border-studio-700 rounded px-2 py-1 text-xs text-white placeholder-slate-500 font-mono mb-1.5"
                    />
                    <div className="flex justify-end space-x-1.5">
                      <button
                        type="button"
                        onClick={() => setShowKeyForm(false)}
                        className="px-2 py-0.5 bg-studio-800 hover:bg-studio-700 text-slate-300 rounded text-[10px]"
                      >
                        Cancel
                      </button>
                      <button
                        type="submit"
                        disabled={isSavingKey || !apiKeyInput.trim()}
                        className="px-2.5 py-0.5 bg-cinema-cyan hover:bg-cyan-500 text-studio-950 font-bold rounded text-[10px] disabled:opacity-50"
                      >
                        {isSavingKey ? 'Saving...' : 'Save & Activate'}
                      </button>
                    </div>
                  </form>
                )}
              </div>

              {/* Ollama Option (Optional Local Fallback) */}
              <div
                onClick={() => handleSwitchProvider('ollama')}
                className={`p-2.5 rounded-lg cursor-pointer mb-2 transition-all border ${
                  aiStatus?.active_provider === 'ollama'
                    ? 'bg-studio-800 border-cinema-gold/40'
                    : 'bg-studio-950/40 border-studio-800 hover:bg-studio-800'
                }`}
              >
                <div className="flex items-start justify-between">
                  <div>
                    <div className="font-semibold text-white flex items-center space-x-1.5">
                      <span>Ollama (Optional Local Fallback)</span>
                      {isOllamaOnline ? (
                        <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
                      ) : (
                        <span className="text-[10px] text-slate-500">
                          {isOllamaEnabled ? 'Offline' : 'Disabled'}
                        </span>
                      )}
                    </div>
                    <p className="text-[11px] text-slate-400 mt-0.5">
                      {isOllamaOnline
                        ? `Online (${aiStatus?.ollama?.configured_model || 'llama3.2'})`
                        : isOllamaEnabled
                        ? 'Ollama not running (http://127.0.0.1:11434)'
                        : 'Local LLM optional fallback'}
                    </p>
                  </div>
                </div>
              </div>

              {/* Offline Template Option */}
              <div
                onClick={() => handleSwitchProvider('offline_template')}
                className={`p-2.5 rounded-lg cursor-pointer transition-all border ${
                  aiStatus?.active_provider === 'offline_template'
                    ? 'bg-studio-800 border-cinema-cyan/40'
                    : 'bg-studio-950/40 border-studio-800 hover:bg-studio-800'
                }`}
              >
                <div>
                  <div className="font-semibold text-white flex items-center space-x-1.5">
                    <span>Offline Studio Engine</span>
                    <CheckCircle2 className="w-3.5 h-3.5 text-cinema-cyan" />
                  </div>
                  <p className="text-[11px] text-slate-400 mt-0.5">
                    Deterministic procedural templates (zero cost fallback)
                  </p>
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Budget Tracker Widget */}
        {activeProject && (
          <button
            onClick={() => onSelectView('cost')}
            className="flex items-center space-x-3 bg-studio-950 border border-studio-800 hover:border-cinema-gold/40 px-3.5 py-1.5 rounded-lg transition-all text-left group"
          >
            <div className="w-7 h-7 rounded bg-studio-850 flex items-center justify-center text-cinema-gold group-hover:scale-105 transition-transform">
              <DollarSign className="w-4 h-4" />
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <span className="text-[10px] uppercase font-mono text-slate-400">Budget Tracker</span>
                <span className="text-[10px] font-mono font-semibold text-emerald-400">
                  ${budgetRemaining.toFixed(2)} left
                </span>
              </div>
              <div className="w-24 bg-studio-800 h-1.5 rounded-full overflow-hidden mt-1">
                <div
                  className={`h-full transition-all duration-300 ${
                    percentSpent > 85 ? 'bg-cinema-crimson' : percentSpent > 60 ? 'bg-cinema-amber' : 'bg-emerald-400'
                  }`}
                  style={{ width: `${percentSpent}%` }}
                />
              </div>
            </div>
          </button>
        )}

        <button
          onClick={onOpenNewProjectModal}
          className="flex items-center space-x-1.5 bg-gradient-to-r from-cinema-amber to-cinema-gold hover:opacity-95 text-studio-950 px-3.5 py-2 rounded-lg text-xs font-semibold shadow-md shadow-cinema-amber/10 transition-all active:scale-95"
        >
          <Plus className="w-4 h-4 stroke-[2.5]" />
          <span>New Film</span>
        </button>
      </div>
    </header>
  );
};

