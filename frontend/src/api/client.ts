// API Client for AI Film Studio - Hybrid Edition

export interface Project {
  id: string;
  name: string;
  logline: string;
  genre: string;
  aspect_ratio: string;
  fps: number;
  target_duration_sec: number;
  visual_style: string;
  budget_total: number;
  budget_spent: number;
  created_at: string;
  updated_at: string;
  scenes_count?: number;
  shots_count?: number;
  ready_shots_count?: number;
  media_count?: number;
}

export interface Character {
  id: string;
  project_id: string;
  name: string;
  role: string;
  age: string;
  facial_characteristics: string;
  clothing: string;
  voice_profile: string;
  personality: string;
  visual_prompt_tags: string;
  reference_image_path: string;
  created_at: string;
}

export interface Location {
  id: string;
  project_id: string;
  name: string;
  description: string;
  time_of_day: string;
  weather: string;
  lighting_setup: string;
  visual_prompt_tags: string;
  reference_image_path: string;
  created_at: string;
}

export interface Scene {
  id: string;
  project_id: string;
  scene_number: number;
  slugline: string;
  title: string;
  summary: string;
  script_content: string;
  location_id: string | null;
  estimated_duration: number;
  budget_limit: number;
  order_index: number;
  created_at: string;
  shots_count?: number;
}

export interface Shot {
  id: string;
  scene_id: string;
  project_id: string;
  shot_number: number;
  shot_code: string;
  duration_seconds: number;
  camera_angle: string;
  camera_movement: string;
  visual_description: string;
  lighting: string;
  character_references: string;
  video_prompt: string;
  negative_prompt: string;
  seed: number | null;
  generation_status: 'pending' | 'ready_for_flow' | 'generating' | 'imported' | 'approved';
  video_asset_id: string | null;
  thumbnail_path: string;
  order_index: number;
  created_at: string;
  media_url?: string | null;
  versions_count?: number;
  selected_version?: number | null;
}

export interface MediaAsset {
  id: string;
  project_id: string;
  shot_id?: string | null;
  version?: number;
  status?: string;
  filename: string;
  file_path: string;
  media_type: string;
  width: number;
  height: number;
  duration_seconds: number;
  fps: number;
  codec: string;
  file_size_bytes: number;
  thumbnail_path: string;
  checksum?: string | null;
  auto_matched_shot_id: string | null;
  is_validated: boolean;
  validation_notes: string;
  created_at: string;
  url?: string;
}

export interface FlowPackageInfo {
  project_name: string;
  total_shots: number;
  total_characters: number;
  estimated_minutes: number;
  instructions_markdown: string;
  quick_copy_txt: string;
  zip_available: boolean;
}

export interface WatcherStatus {
  watch_directory_path: string;
  pending_files_count: number;
  pending_files: string[];
  supported_extensions: string[];
}

export interface TimelineItem {
  shot_id: string;
  scene_id: string;
  shot_number: number;
  shot_code: string;
  camera_angle: string;
  camera_movement: string;
  visual_description: string;
  duration_seconds: number;
  start_time_seconds: number;
  end_time_seconds: number;
  generation_status: string;
  media_url?: string | null;
  thumbnail_url?: string | null;
}

export interface TimelineData {
  project_id: string;
  project_name: string;
  aspect_ratio: string;
  total_duration_seconds: number;
  total_shots_count: number;
  timeline_items: TimelineItem[];
}

export interface RenderResult {
  output_path: string;
  filename: string;
  duration_seconds: number;
  resolution: string;
  fps: number;
  file_size_bytes: number;
  video_url: string;
}

export interface AudioTrack {
  id: string;
  project_id: string;
  track_type: 'voiceover' | 'music' | 'sfx' | 'ambient';
  title: string;
  file_path: string;
  duration_seconds: number;
  start_time_seconds: number;
  volume_level: number;
  ducking_enabled: boolean;
  ducking_level: number;
  scene_id?: string | null;
  shot_id?: string | null;
  created_at: string;
  url?: string;
}

export interface ExportJob {
  id: string;
  project_id: string;
  preset_name: string;
  resolution: string;
  aspect_ratio: string;
  fps: number;
  include_subtitles: boolean;
  render_mode?: 'final' | 'preview' | string;
  allow_placeholders?: boolean;
  status: 'queued' | 'rendering' | 'completed' | 'failed' | 'cancelled' | 'processing';
  progress_pct: number;
  output_path: string;
  file_size_bytes: number;
  error_message?: string;
  created_at: string;
  completed_at?: string;
  download_url?: string;
}

export interface PresetConfig {
  name: string;
  resolution: string;
  aspect_ratio: string;
  fps: number;
  description: string;
}

export interface ContinuityReport {
  status: 'PASS' | 'WARNING';
  score: number;
  issues: string[];
  suggestions: string[];
}

export interface DashboardStats {
  total_projects: number;
  active_project_id: string | null;
  total_scenes: number;
  total_shots: number;
  total_ready_shots: number;
  total_media_imported: number;
  budget_allocated: number;
  budget_spent: number;
  recent_activity: Array<{
    id: string;
    name: string;
    genre: string;
    updated_at: string;
  }>;
}

export interface CostRecordItem {
  id: string;
  project_id?: string;
  scene_id?: string | null;
  shot_id?: string | null;
  provider?: string;
  model?: string | null;
  operation?: string;
  input_tokens?: number;
  output_tokens?: number;
  flow_credits?: number;
  actual_cost?: number | null;
  currency?: string;
  estimated_cost?: number;
  actual_cost_known?: boolean;
  action_type: string;
  credits_used: number;
  cost_usd: number;
  details: string;
  created_at: string;
}

export interface BudgetSummary {
  project_id: string;
  budget_total: number;
  budget_spent: number;
  budget_remaining: number;
  percent_used: number;
  total_actions: number;
  total_flow_credits?: number;
  provider_breakdown?: Record<string, number>;
  records: CostRecordItem[];
}

const API_BASE = '/api';

export class ApiClientError extends Error {
  status: number;
  detail: string;
  body?: any;

  constructor(status: number, detail: string, body?: any) {
    super(detail);
    this.name = 'ApiClientError';
    this.status = status;
    this.detail = detail;
    this.body = body;
    Object.setPrototypeOf(this, ApiClientError.prototype);
  }
}

export async function parseApiResponse<T>(res: Response, fallbackMessage: string): Promise<T> {
  if (res.ok) {
    const text = await res.text();
    if (!text) {
      return {} as T;
    }
    try {
      return JSON.parse(text);
    } catch {
      return text as unknown as T;
    }
  }

  let body: any = {};
  try {
    body = await res.json();
  } catch {
    // Response body is not JSON
  }

  let detail: string = body.detail || body.message || '';
  if (typeof detail === 'object') {
    detail = JSON.stringify(detail);
  }

  if (!detail) {
    if (res.status === 403) {
      detail = 'Approval required: action is blocked by Gatekeeper until prerequisite checkpoint is approved.';
    } else if (res.status === 409) {
      detail = 'Conflict: artifact state or version has changed concurrently. Please refresh and retry.';
    } else if (res.status === 422) {
      detail = 'Validation error: invalid request payload.';
    } else if (res.status >= 500) {
      detail = 'Server error: an internal server error occurred.';
    } else {
      detail = fallbackMessage;
    }
  }

  throw new ApiClientError(res.status, detail, body);
}

export async function fetchProjects(): Promise<Project[]> {
  const res = await fetch(`${API_BASE}/projects`);
  if (!res.ok) throw new Error('Failed to fetch projects');
  return res.json();
}

export async function createProject(data: Partial<Project>): Promise<Project> {
  const res = await fetch(`${API_BASE}/projects`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error('Failed to create project');
  return res.json();
}

export async function updateProject(id: string, data: Partial<Project>): Promise<Project> {
  const res = await fetch(`${API_BASE}/projects/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error('Failed to update project');
  return res.json();
}

export async function deleteProject(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/projects/${id}`, { method: 'DELETE' });
  if (!res.ok) throw new Error('Failed to delete project');
}

export async function fetchDashboardStats(): Promise<DashboardStats> {
  const res = await fetch(`${API_BASE}/dashboard/stats`);
  if (!res.ok) throw new Error('Failed to fetch dashboard stats');
  return res.json();
}

export async function fetchBudgetSummary(projectId: string): Promise<BudgetSummary> {
  const res = await fetch(`${API_BASE}/cost/summary/${projectId}`);
  if (!res.ok) throw new Error('Failed to fetch budget summary');
  return res.json();
}

export async function topUpBudget(projectId: string, amount: number): Promise<any> {
  const res = await fetch(`${API_BASE}/cost/top-up/${projectId}?amount=${amount}`, {
    method: 'POST'
  });
  if (!res.ok) throw new Error('Failed to top up budget');
  return res.json();
}

// Character API methods
export async function fetchCharacters(projectId: string): Promise<Character[]> {
  const res = await fetch(`${API_BASE}/characters/${projectId}`);
  if (!res.ok) throw new Error('Failed to fetch characters');
  return res.json();
}

export async function createCharacter(data: Partial<Character>): Promise<Character> {
  const res = await fetch(`${API_BASE}/characters`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error('Failed to create character');
  return res.json();
}

export async function generateCharacter(projectId: string, role: string, conceptPrompt?: string): Promise<Character> {
  const res = await fetch(`${API_BASE}/characters/ai-generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_id: projectId, role, concept_prompt: conceptPrompt }),
  });
  if (!res.ok) throw new Error('Failed to generate character');
  return res.json();
}

export async function updateCharacter(id: string, data: Partial<Character>): Promise<Character> {
  const res = await fetch(`${API_BASE}/characters/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error('Failed to update character');
  return res.json();
}

export async function deleteCharacter(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/characters/${id}`, { method: 'DELETE' });
  if (!res.ok) throw new Error('Failed to delete character');
}

export async function uploadCharacterRef(id: string, file: File): Promise<any> {
  const formData = new FormData();
  formData.append('file', file);
  const res = await fetch(`${API_BASE}/characters/${id}/upload-ref`, {
    method: 'POST',
    body: formData,
  });
  if (!res.ok) throw new Error('Failed to upload character reference');
  return res.json();
}

// Location API methods
export async function fetchLocations(projectId: string): Promise<Location[]> {
  const res = await fetch(`${API_BASE}/locations/${projectId}`);
  if (!res.ok) throw new Error('Failed to fetch locations');
  return res.json();
}

export async function createLocation(data: Partial<Location>): Promise<Location> {
  const res = await fetch(`${API_BASE}/locations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error('Failed to create location');
  return res.json();
}

export async function generateLocation(projectId: string, timeOfDay: string, atmospherePrompt?: string): Promise<Location> {
  const res = await fetch(`${API_BASE}/locations/ai-generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_id: projectId, time_of_day: timeOfDay, atmosphere_prompt: atmospherePrompt }),
  });
  if (!res.ok) throw new Error('Failed to generate location');
  return res.json();
}

export async function updateLocation(id: string, data: Partial<Location>): Promise<Location> {
  const res = await fetch(`${API_BASE}/locations/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error('Failed to update location');
  return res.json();
}

export async function deleteLocation(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/locations/${id}`, { method: 'DELETE' });
  if (!res.ok) throw new Error('Failed to delete location');
}

export async function uploadLocationRef(id: string, file: File): Promise<any> {
  const formData = new FormData();
  formData.append('file', file);
  const res = await fetch(`${API_BASE}/locations/${id}/upload-ref`, {
    method: 'POST',
    body: formData,
  });
  if (!res.ok) throw new Error('Failed to upload location reference');
  return res.json();
}
export async function fetchScenes(projectId: string): Promise<Scene[]> {
  const res = await fetch(`${API_BASE}/scenes/${projectId}`);
  return parseApiResponse<Scene[]>(res, 'Failed to fetch scenes');
}

export async function createScene(data: Partial<Scene>): Promise<Scene> {
  const res = await fetch(`${API_BASE}/scenes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return parseApiResponse<Scene>(res, 'Failed to create scene');
}

export async function generateScene(projectId: string, prompt: string): Promise<Scene> {
  const res = await fetch(`${API_BASE}/scenes/generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_id: projectId, prompt }),
  });
  return parseApiResponse<Scene>(res, 'Failed to generate scene');
}

export async function updateScene(id: string, data: Partial<Scene>): Promise<Scene> {
  const res = await fetch(`${API_BASE}/scenes/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return parseApiResponse<Scene>(res, 'Failed to update scene');
}

export async function deleteScene(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/scenes/${id}`, { method: 'DELETE' });
  return parseApiResponse<void>(res, 'Failed to delete scene');
}

export async function refineScene(id: string, direction: string): Promise<any> {
  const res = await fetch(`${API_BASE}/scenes/${id}/refine`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ direction }),
  });
  return parseApiResponse<any>(res, 'Failed to refine scene');
}

// Storyboard API methods
export async function fetchShots(projectId: string, sceneId?: string): Promise<Shot[]> {
  const url = sceneId ? `${API_BASE}/storyboard/${projectId}?scene_id=${sceneId}` : `${API_BASE}/storyboard/${projectId}`;
  const res = await fetch(url);
  return parseApiResponse<Shot[]>(res, 'Failed to fetch shots');
}

export async function createShot(data: Partial<Shot>): Promise<Shot> {
  const res = await fetch(`${API_BASE}/storyboard`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return parseApiResponse<Shot>(res, 'Failed to create shot');
}

export async function generateShotsForScene(sceneId: string, shotCount: number = 4): Promise<Shot[]> {
  const res = await fetch(`${API_BASE}/storyboard/generate/${sceneId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ shot_count: shotCount }),
  });
  return parseApiResponse<Shot[]>(res, 'Failed to generate shots');
}

export async function updateShot(id: string, data: Partial<Shot>): Promise<Shot> {
  const res = await fetch(`${API_BASE}/storyboard/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return parseApiResponse<Shot>(res, 'Failed to update shot');
}

export async function deleteShot(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/storyboard/${id}`, { method: 'DELETE' });
  return parseApiResponse<void>(res, 'Failed to delete shot');
}

export async function optimizeShotPrompt(id: string, characterAnchors?: string): Promise<Shot> {
  const res = await fetch(`${API_BASE}/storyboard/${id}/optimize-prompt`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ character_anchors: characterAnchors }),
  });
  return parseApiResponse<Shot>(res, 'Failed to optimize shot prompt');
}

export async function runContinuityCheck(projectId: string): Promise<ContinuityReport> {
  const res = await fetch(`${API_BASE}/storyboard/continuity-check/${projectId}`, {
    method: 'POST',
  });
  return parseApiResponse<ContinuityReport>(res, 'Failed to run continuity check');
}

// Flow Handoff API methods
export async function fetchFlowPackageInfo(projectId: string): Promise<FlowPackageInfo> {
  const res = await fetch(`${API_BASE}/flow-handoff/package-info/${projectId}`);
  if (!res.ok) throw new Error('Failed to fetch Flow package info');
  return res.json();
}

export function getFlowPackageDownloadUrl(projectId: string): string {
  return `${API_BASE}/flow-handoff/download/${projectId}`;
}

// Media API methods
export async function fetchMediaAssets(projectId: string): Promise<MediaAsset[]> {
  const res = await fetch(`${API_BASE}/media/assets/${projectId}`);
  if (!res.ok) throw new Error('Failed to fetch media assets');
  return res.json();
}

export async function uploadMedia(projectId: string, file: File): Promise<MediaAsset> {
  const formData = new FormData();
  formData.append('file', file);
  const res = await fetch(`${API_BASE}/media/upload/${projectId}`, {
    method: 'POST',
    body: formData,
  });
  if (!res.ok) throw new Error('Failed to upload video file');
  return res.json();
}

export async function scanWatchedFolder(projectId: string): Promise<any> {
  const res = await fetch(`${API_BASE}/media/scan-watched/${projectId}`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error('Failed to scan watched folder');
  return res.json();
}

export async function fetchWatcherStatus(): Promise<WatcherStatus> {
  const res = await fetch(`${API_BASE}/media/watcher-status`);
  if (!res.ok) throw new Error('Failed to fetch watcher status');
  return res.json();
}

export async function linkAssetToShot(assetId: string, shotId: string): Promise<any> {
  const res = await fetch(`${API_BASE}/media/link-shot/${assetId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ shot_id: shotId }),
  });
  if (!res.ok) throw new Error('Failed to link asset to shot');
  return res.json();
}

export async function deleteMediaAsset(assetId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/media/${assetId}`, { method: 'DELETE' });
  if (!res.ok) throw new Error('Failed to delete media asset');
}

export async function generateSyntheticTestClip(projectId: string, shotCode: string = 'SC01_SH01'): Promise<any> {
  const res = await fetch(`${API_BASE}/media/generate-test-clip/${projectId}?shot_code=${shotCode}&duration=3.0`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error('Failed to generate test clip');
  return res.json();
}

// Editor API methods
export async function fetchTimeline(projectId: string): Promise<TimelineData> {
  const res = await fetch(`${API_BASE}/editor/timeline/${projectId}`);
  if (!res.ok) throw new Error('Failed to fetch timeline');
  return res.json();
}

export async function runAiAssemblyCut(projectId: string): Promise<any> {
  const res = await fetch(`${API_BASE}/editor/ai-assembly-cut/${projectId}`, {
    method: 'POST',
  });
  if (!res.ok) throw new Error('Failed to run AI assembly cut');
  return res.json();
}

export async function renderTimelinePreview(
  projectId: string,
  resolution: string = '1280x720',
  fps: number = 24,
  burnSubtitles: boolean = true
): Promise<RenderResult> {
  const res = await fetch(`${API_BASE}/editor/render-preview/${projectId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ resolution, fps, burn_subtitles: burnSubtitles }),
  });
  if (!res.ok) throw new Error('Failed to render timeline preview');
  return res.json();
}

// Audio API methods
export async function fetchAudioTracks(projectId: string): Promise<AudioTrack[]> {
  const res = await fetch(`${API_BASE}/audio/${projectId}`);
  if (!res.ok) throw new Error('Failed to fetch audio tracks');
  return res.json();
}

export async function uploadAudio(
  projectId: string,
  file: File,
  trackType: string = 'music',
  title: string = 'Audio Track'
): Promise<AudioTrack> {
  const formData = new FormData();
  formData.append('file', file);
  const res = await fetch(
    `${API_BASE}/audio/upload/${projectId}?track_type=${trackType}&title=${encodeURIComponent(title)}`,
    {
      method: 'POST',
      body: formData,
    }
  );
  if (!res.ok) throw new Error('Failed to upload audio');
  return res.json();
}

export async function generateBgm(
  projectId: string,
  title: string = 'Cinematic Ambient Score',
  duration: number = 20.0,
  mood: string = 'cyberpunk'
): Promise<AudioTrack> {
  const res = await fetch(`${API_BASE}/audio/generate-bgm/${projectId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title, duration, mood }),
  });
  if (!res.ok) throw new Error('Failed to generate BGM');
  return res.json();
}

export async function generateVoiceover(
  projectId: string,
  characterName: string,
  dialogueText: string,
  duration: number = 4.0
): Promise<AudioTrack> {
  const res = await fetch(`${API_BASE}/audio/generate-voiceover/${projectId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      character_name: characterName,
      dialogue_text: dialogueText,
      duration,
    }),
  });
  if (!res.ok) throw new Error('Failed to generate voiceover');
  return res.json();
}

export async function updateAudioTrack(
  trackId: string,
  data: Partial<AudioTrack>
): Promise<AudioTrack> {
  const res = await fetch(`${API_BASE}/audio/${trackId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error('Failed to update audio track');
  return res.json();
}

export async function deleteAudioTrack(trackId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/audio/${trackId}`, { method: 'DELETE' });
  if (!res.ok) throw new Error('Failed to delete audio track');
}

// Export API methods
export async function fetchExportPresets(): Promise<Record<string, PresetConfig>> {
  const res = await fetch(`${API_BASE}/export/presets`);
  if (!res.ok) throw new Error('Failed to fetch export presets');
  return res.json();
}

export async function fetchExportJobs(projectId: string): Promise<ExportJob[]> {
  const res = await fetch(`${API_BASE}/export/jobs/${projectId}`);
  if (!res.ok) throw new Error('Failed to fetch export jobs');
  return res.json();
}

export async function launchExportRender(
  projectId: string,
  presetName: string = '1080p_cinematic',
  resolution: string = '1920x1080',
  fps: number = 24,
  includeSubtitles: boolean = true,
  renderMode: 'preview' | 'final' = 'final',
  allowPlaceholders: boolean = false
): Promise<ExportJob> {
  const res = await fetch(`${API_BASE}/export/render/${projectId}?async=true`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      project_id: projectId,
      preset_name: presetName,
      resolution,
      fps,
      include_subtitles: includeSubtitles,
      render_mode: renderMode,
      allow_placeholders: allowPlaceholders,
    }),
  });
  return parseApiResponse<ExportJob>(res, 'Failed to launch export render');
}

// ---------------------------------------------------------------------------
// S4 M2.2 Story Approval & Gatekeeper Client Methods
// ---------------------------------------------------------------------------

export interface StoryReviewSceneItem {
  scene_number: number;
  order_index: number;
  slugline: string;
  title: string;
  summary: string;
  script_content: string;
  estimated_duration: number;
}

export interface StoryReviewData {
  project_id: string;
  project_name: string;
  checkpoint_status: string;
  artifact_version: number;
  current_artifact_hash: string | null;
  submitted_artifact_hash: string | null;
  is_hash_fresh: boolean;
  scenes: StoryReviewSceneItem[];
}

export interface StorySubmitResponse {
  project_id: string;
  checkpoint_id: string;
  checkpoint_type: string;
  status: string;
  artifact_version: number;
  artifact_content_hash: string;
  submitted_at: string;
  message: string;
}

export interface CheckpointDecisionPayload {
  decision: 'APPROVED' | 'REJECTED' | 'REQUEST_CHANGES';
  expected_artifact_version: number;
  expected_artifact_hash: string;
  reviewer_name: string;
  feedback_notes?: string;
}

export interface CheckpointDecisionResponse {
  project_id: string;
  checkpoint_id: string;
  checkpoint_type: string;
  status: string;
  decision: string;
  artifact_version: number;
  artifact_content_hash: string;
  reviewer_name: string;
  feedback_notes?: string;
  decided_at: string;
}

export interface PipelineCheckpointDetail {
  id: string;
  project_id: string;
  checkpoint_type: string;
  status: string;
  artifact_type: string;
  artifact_version: number;
  artifact_content_hash: string | null;
  current_artifact_hash?: string | null;
  is_hash_fresh?: boolean;
  reviewer_name: string;
  feedback_notes?: string;
  decided_at?: string;
  created_at: string;
  updated_at: string;
}

export async function fetchStoryReview(projectId: string): Promise<StoryReviewData> {
  const res = await fetch(`${API_BASE}/pipeline/${projectId}/checkpoints/story_approval/review`);
  return parseApiResponse<StoryReviewData>(res, 'Failed to fetch story review data');
}

export async function submitStoryForReview(projectId: string): Promise<StorySubmitResponse> {
  const res = await fetch(`${API_BASE}/pipeline/${projectId}/checkpoints/story_approval/submit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
  return parseApiResponse<StorySubmitResponse>(res, 'Failed to submit story for review');
}

export async function decideStoryApproval(
  projectId: string,
  payload: CheckpointDecisionPayload
): Promise<CheckpointDecisionResponse> {
  const res = await fetch(`${API_BASE}/pipeline/${projectId}/checkpoints/story_approval/decision`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return parseApiResponse<CheckpointDecisionResponse>(res, 'Failed to record decision');
}

export async function fetchCheckpointDetail(
  projectId: string,
  checkpointType: string
): Promise<PipelineCheckpointDetail> {
  const res = await fetch(`${API_BASE}/pipeline/${projectId}/checkpoints/${checkpointType}`);
  return parseApiResponse<PipelineCheckpointDetail>(res, `Failed to fetch checkpoint ${checkpointType}`);
}

export interface AIStatusResponse {
  active_provider: string;
  ollama: {
    provider: string;
    status: string;
    host?: string;
    configured_model?: string;
    model_available?: boolean;
    available_models?: string[];
    error?: string;
  };
  gemini: {
    provider: string;
    status: string;
    configured_model?: string;
    has_client?: boolean;
    note?: string;
  };
  offline_template: {
    provider: string;
    status: string;
    configured_model: string;
  };
}

export async function fetchAIStatus(): Promise<AIStatusResponse> {
  const res = await fetch(`${API_BASE}/system/ai-status`);
  return parseApiResponse<AIStatusResponse>(res, 'Failed to fetch AI engine status');
}

export async function updateAIConfig(data: { provider?: string; ollama_host?: string; ollama_model?: string }): Promise<any> {
  const res = await fetch(`${API_BASE}/system/ai-config`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return parseApiResponse<any>(res, 'Failed to update AI configuration');
}

export async function fetchShotVersions(shotId: string): Promise<MediaAsset[]> {
  const res = await fetch(`${API_BASE}/storyboard/shot/${shotId}/versions`);
  return parseApiResponse<MediaAsset[]>(res, 'Failed to fetch shot versions');
}

export async function selectShotVersion(shotId: string, mediaAssetId: string): Promise<any> {
  const res = await fetch(`${API_BASE}/storyboard/shot/${shotId}/select-version/${mediaAssetId}`, {
    method: 'POST',
  });
  return parseApiResponse<any>(res, 'Failed to select shot version');
}

export async function backupProject(projectId: string): Promise<{ status: string; backup_filename: string; download_url: string; size_bytes: number }> {
  const res = await fetch(`${API_BASE}/system/projects/${projectId}/backup`, {
    method: 'POST',
  });
  return parseApiResponse(res, 'Failed to create project backup');
}

export async function restoreProject(file: File, newName?: string, overwrite?: boolean): Promise<any> {
  const form = new FormData();
  form.append('file', file);
  if (newName) form.append('new_project_name', newName);
  if (overwrite !== undefined) form.append('overwrite', String(overwrite));
  const res = await fetch(`${API_BASE}/system/projects/restore`, {
    method: 'POST',
    body: form,
  });
  return parseApiResponse(res, 'Failed to restore project package');
}

export async function fetchExportJob(jobId: string): Promise<ExportJob> {
  const res = await fetch(`${API_BASE}/export/job/${jobId}`);
  return parseApiResponse<ExportJob>(res, 'Failed to fetch export job status');
}

export async function cancelExportJob(jobId: string): Promise<any> {
  const res = await fetch(`${API_BASE}/export/job/${jobId}/cancel`, {
    method: 'POST',
  });
  return parseApiResponse(res, 'Failed to cancel export job');
}

export async function retryExportJob(jobId: string): Promise<ExportJob> {
  const res = await fetch(`${API_BASE}/export/job/${jobId}/retry`, {
    method: 'POST',
  });
  return parseApiResponse<ExportJob>(res, 'Failed to retry export job');
}


