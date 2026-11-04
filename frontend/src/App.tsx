import React, { useState, useEffect } from 'react';
import {
  Project,
  DashboardStats,
  fetchProjects,
  createProject,
  fetchDashboardStats,
} from './api/client';
import { Sidebar, ViewTab } from './components/Sidebar';
import { Header } from './components/Header';
import { NewProjectModal } from './components/NewProjectModal';
import { DashboardView } from './views/DashboardView';
import { ProjectsView } from './views/ProjectsView';
import { CostControllerView } from './views/CostControllerView';
import { ScreenplayView } from './views/ScreenplayView';
import { StoryboardView } from './views/StoryboardView';
import { CharacterView } from './views/CharacterView';
import { LocationView } from './views/LocationView';
import { FlowHandoffView } from './views/FlowHandoffView';
import { MediaLibraryView } from './views/MediaLibraryView';
import { TimelineEditorView } from './views/TimelineEditorView';
import { AudioManagerView } from './views/AudioManagerView';
import { ExportCenterView } from './views/ExportCenterView';

export const App: React.FC = () => {
  const [currentView, setCurrentView] = useState<ViewTab>('dashboard');
  const [projects, setProjects] = useState<Project[]>([]);
  const [activeProject, setActiveProject] = useState<Project | null>(null);
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [loading, setLoading] = useState(true);

  const handleSelectProject = (project: Project | null) => {
    setActiveProject(project);
    if (project) {
      localStorage.setItem('ai_film_studio_active_project_id', project.id);
    } else {
      localStorage.removeItem('ai_film_studio_active_project_id');
    }
  };

  const loadProjects = async () => {
    try {
      const list = await fetchProjects();
      setProjects(list);
      if (list.length > 0) {
        setActiveProject((prev) => {
          if (prev) {
            const found = list.find((p) => p.id === prev.id);
            if (found) return found;
          }
          const savedId = localStorage.getItem('ai_film_studio_active_project_id');
          if (savedId) {
            const found = list.find((p) => p.id === savedId);
            if (found) return found;
          }
          return list[0];
        });
      }
    } catch (err) {
      console.error('Failed to load projects:', err);
    }
  };

  const loadStats = async () => {
    try {
      const s = await fetchDashboardStats();
      setStats(s);
    } catch (err) {
      console.error('Failed to load stats:', err);
    }
  };

  useEffect(() => {
    const init = async () => {
      setLoading(true);
      await Promise.all([loadProjects(), loadStats()]);
      setLoading(false);
    };
    init();
  }, []);

  const handleCreateProject = async (data: Partial<Project>) => {
    const created = await createProject(data);
    await loadProjects();
    setActiveProject(created);
    await loadStats();
  };

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-studio-950 text-slate-100 font-sans">
      {/* Studio Navigation Sidebar */}
      <Sidebar
        currentView={currentView}
        onSelectView={setCurrentView}
        projects={projects}
        activeProject={activeProject}
        onSelectProject={handleSelectProject}
        onOpenNewProjectModal={() => setIsModalOpen(true)}
      />

      {/* Main Studio Viewport */}
      <div className="flex-1 flex flex-col h-full overflow-hidden">
        <Header
          activeProject={activeProject}
          onOpenNewProjectModal={() => setIsModalOpen(true)}
          onSelectView={setCurrentView}
        />

        <main className="flex-1 overflow-y-auto bg-studio-950">
          {loading ? (
            <div className="h-full flex items-center justify-center space-x-3 text-slate-400">
              <div className="w-5 h-5 border-2 border-cinema-gold border-t-transparent rounded-full animate-spin" />
              <span className="text-sm font-mono">Initializing Studio Engine...</span>
            </div>
          ) : (
            <>
              {currentView === 'dashboard' && (
                <DashboardView
                  activeProject={activeProject}
                  projects={projects}
                  stats={stats}
                  onSelectView={setCurrentView}
                  onSelectProject={handleSelectProject}
                  onOpenNewProjectModal={() => setIsModalOpen(true)}
                />
              )}

              {currentView === 'projects' && (
                <ProjectsView
                  projects={projects}
                  activeProject={activeProject}
                  onSelectProject={handleSelectProject}
                  onRefresh={loadProjects}
                  onOpenNewProjectModal={() => setIsModalOpen(true)}
                />
              )}

              {currentView === 'cost' && (
                <CostControllerView
                  activeProject={activeProject}
                  onRefreshProject={loadProjects}
                />
              )}

              {currentView === 'screenplay' && (
                <ScreenplayView
                  activeProject={activeProject}
                  onSelectView={setCurrentView}
                  onRefreshProject={loadProjects}
                />
              )}

              {currentView === 'storyboard' && (
                <StoryboardView
                  activeProject={activeProject}
                  onRefreshProject={loadProjects}
                  onSelectView={setCurrentView}
                />
              )}

              {currentView === 'characters' && (
                <CharacterView
                  activeProject={activeProject}
                  onRefreshProject={loadProjects}
                />
              )}

              {currentView === 'locations' && (
                <LocationView
                  activeProject={activeProject}
                  onRefreshProject={loadProjects}
                />
              )}

              {currentView === 'flow_handoff' && (
                <FlowHandoffView
                  activeProject={activeProject}
                  onSelectView={setCurrentView}
                />
              )}

              {currentView === 'media_import' && (
                <MediaLibraryView
                  activeProject={activeProject}
                  onRefreshProject={loadProjects}
                />
              )}

              {currentView === 'editor' && (
                <TimelineEditorView
                  activeProject={activeProject}
                  onRefreshProject={loadProjects}
                />
              )}

              {currentView === 'audio' && (
                <AudioManagerView
                  activeProject={activeProject}
                  onRefreshProject={loadProjects}
                />
              )}

              {currentView === 'export' && (
                <ExportCenterView
                  activeProject={activeProject}
                  onRefreshProject={loadProjects}
                />
              )}
            </>
          )}
        </main>
      </div>

      {/* Project Creation Modal */}
      <NewProjectModal
        isOpen={isModalOpen}
        onClose={() => setIsModalOpen(false)}
        onCreate={handleCreateProject}
      />
    </div>
  );
};
export default App;
