import { useCallback, useEffect, useState } from 'react'
import type {
  AngleTarget,
  ApiResponse,
  BandwidthEstimate,
  BandwidthMatrix,
  CalibrationProfile,
  CameraControlRequest,
  CameraStatus,
  CaptureAngleTargetRequest,
  Detection,
  IMUData,
  StreamSettingsRequest,
} from '../types'

const API = '/api'

export function useCameraList(pollMs = 3000) {
  const [cameras, setCameras] = useState<CameraStatus[]>([])
  const [loading, setLoading] = useState(true)

  const fetchCameras = useCallback(async () => {
    try {
      const res = await fetch(`${API}/cameras`)
      if (!res.ok) return
      const data = await res.json()
      setCameras(data.cameras)
    } catch {
      // network error — keep previous state
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchCameras()
    const id = setInterval(fetchCameras, pollMs)
    return () => clearInterval(id)
  }, [fetchCameras, pollMs])

  const discover = useCallback(async () => {
    await fetch(`${API}/cameras/discover`, { method: 'POST' })
    await fetchCameras()
  }, [fetchCameras])

  return { cameras, loading, discover, refresh: fetchCameras }
}

export function useDetections(cameraId: string, pollMs = 200) {
  const [detection, setDetection] = useState<Detection | null>(null)

  useEffect(() => {
    if (!cameraId) return
    const id = setInterval(async () => {
      try {
        const res = await fetch(`${API}/camera/${cameraId}/detections`)
        if (res.ok) {
          const data = await res.json()
          setDetection(data)
        }
      } catch { /* ignore */ }
    }, pollMs)
    return () => clearInterval(id)
  }, [cameraId, pollMs])

  return detection
}

export function useStreamUrl(cameraId: string) {
  return `${API}/camera/${cameraId}/stream`
}

export function useImuData(cameraId: string, pollMs = 500) {
  const [imu, setImu] = useState<IMUData | null>(null)

  useEffect(() => {
    if (!cameraId) return
    const id = setInterval(async () => {
      try {
        const res = await fetch(`${API}/camera/${cameraId}/imu`)
        if (res.ok) {
          const data: IMUData = await res.json()
          setImu(data.has_data ? data : null)
        }
      } catch { /* ignore */ }
    }, pollMs)
    return () => clearInterval(id)
  }, [cameraId, pollMs])

  return imu
}

// ---------------------------------------------------------------------------
// Camera enable / disable (free bandwidth)
// ---------------------------------------------------------------------------

export async function enableCamera(cameraId: string) {
  const res = await fetch(`${API}/camera/${cameraId}/enable`, { method: 'POST' })
  return res.json()
}

export async function disableCamera(cameraId: string) {
  const res = await fetch(`${API}/camera/${cameraId}/disable`, { method: 'POST' })
  return res.json()
}

// ---------------------------------------------------------------------------
// Single-camera actions
// ---------------------------------------------------------------------------

export async function applyControl(cameraId: string, control: Record<string, unknown>) {
  const res = await fetch(`${API}/camera/${cameraId}/control`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(control),
  })
  return res.json()
}

export async function setStreamSettings(cameraId: string, settings: StreamSettingsRequest) {
  const res = await fetch(`${API}/camera/${cameraId}/stream-settings`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(settings),
  })
  return res.json()
}

export async function startRecording(
  cameraId: string,
  mode: 'video' | 'interval' | 'scheduled',
  intervalSeconds = 5,
  outputDir?: string,
  filenamePrefix?: string,
  clipDurationSeconds?: number,
  clipIntervalSeconds?: number,
): Promise<{ ok: boolean; message: string; data?: string }> {
  const res = await fetch(`${API}/camera/${cameraId}/recording/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode,
      interval_seconds: intervalSeconds,
      output_dir: outputDir || null,
      filename_prefix: filenamePrefix || '',
      clip_duration_seconds: clipDurationSeconds ?? 5,
      clip_interval_seconds: clipIntervalSeconds ?? 80,
    }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({ message: res.statusText }))
    throw new Error(body?.detail ?? body?.message ?? res.statusText)
  }
  return res.json()
}

export async function stopRecording(cameraId: string) {
  const res = await fetch(`${API}/camera/${cameraId}/recording/stop`, { method: 'POST' })
  return res.json()
}

export async function setInferenceMode(
  cameraId: string,
  mode: 'none' | 'on_camera' | 'host',
  modelPath?: string
) {
  const res = await fetch(`${API}/camera/${cameraId}/inference/mode`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode, model_path: modelPath }),
  })
  return res.json()
}

export async function getSnapshot(cameraId: string): Promise<string> {
  const res = await fetch(`${API}/camera/${cameraId}/snapshot`)
  const blob = await res.blob()
  return URL.createObjectURL(blob)
}

// ---------------------------------------------------------------------------
// Bulk actions (apply to ALL cameras)
// ---------------------------------------------------------------------------

export async function applyControlAll(control: Record<string, unknown>) {
  const res = await fetch(`${API}/cameras/control`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(control),
  })
  return res.json()
}

export async function setStreamSettingsAll(settings: StreamSettingsRequest) {
  const res = await fetch(`${API}/cameras/stream-settings`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(settings),
  })
  return res.json()
}

export async function startRecordingAll(
  mode: 'video' | 'interval' | 'scheduled' | 'sequential',
  intervalSeconds = 5,
  outputDir?: string,
  filenamePrefix?: string,
  clipDurationSeconds?: number,
  clipIntervalSeconds?: number,
  interCameraGapSeconds?: number,
  imuChangeThresholdDeg?: number,
  imuSettleSeconds?: number,
) {
  const res = await fetch(`${API}/cameras/recording/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode,
      interval_seconds: intervalSeconds,
      output_dir: outputDir || null,
      filename_prefix: filenamePrefix || '',
      clip_duration_seconds: clipDurationSeconds ?? 5,
      clip_interval_seconds: clipIntervalSeconds ?? 80,
      inter_camera_gap_seconds: interCameraGapSeconds ?? 5,
      imu_change_threshold_deg: imuChangeThresholdDeg ?? 5,
      imu_settle_seconds: imuSettleSeconds ?? 3,
    }),
  })
  return res.json()
}

export async function getFleetRecordingStatus(): Promise<{ sequential_active: boolean; any_recording: boolean }> {
  try {
    const res = await fetch(`${API}/cameras/recording/status`)
    if (!res.ok) return { sequential_active: false, any_recording: false }
    const data = await res.json()
    return data.data ?? { sequential_active: false, any_recording: false }
  } catch {
    return { sequential_active: false, any_recording: false }
  }
}

export async function stopRecordingAll() {
  const res = await fetch(`${API}/cameras/recording/stop`, { method: 'POST' })
  return res.json()
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Last-session persistence
// ---------------------------------------------------------------------------

export interface CameraSessionSnapshot {
  id: string
  stream_fps?: number
  mjpeg_quality?: number
  resolution?: string
  stereo_mode?: string
  flip_180?: boolean
  inference_mode?: string
  controls?: Record<string, unknown>
}

export interface SessionSnapshot {
  saved_at: string
  cameras: CameraSessionSnapshot[]
  recording?: {
    mode?: 'video' | 'interval' | 'scheduled'
    interval_seconds?: number
    clip_duration_seconds?: number
    clip_interval_seconds?: number
    filename_prefix?: string
    output_dir?: string
  }
}

export async function getLastSession(): Promise<SessionSnapshot | null> {
  try {
    const res = await fetch(`${API}/session/last`)
    if (!res.ok) return null
    const data = await res.json()
    return (data?.data ?? null) as SessionSnapshot | null
  } catch {
    return null
  }
}

export async function saveSession(snapshot: SessionSnapshot): Promise<void> {
  try {
    await fetch(`${API}/session/save`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(snapshot),
    })
  } catch { /* ignore */ }
}

export async function applySessionToCameras(snapshot: SessionSnapshot): Promise<void> {
  for (const cam of snapshot.cameras ?? []) {
    const streamReq: StreamSettingsRequest = {}
    if (cam.stream_fps !== undefined) streamReq.fps = cam.stream_fps
    if (cam.mjpeg_quality !== undefined) streamReq.mjpeg_quality = cam.mjpeg_quality
    if (cam.resolution !== undefined) streamReq.resolution = cam.resolution
    if (cam.stereo_mode !== undefined) streamReq.stereo_mode = cam.stereo_mode as never
    if (cam.flip_180 !== undefined) streamReq.flip_180 = cam.flip_180
    if (Object.keys(streamReq).length > 0) {
      try { await setStreamSettings(cam.id, streamReq) } catch { /* ignore */ }
    }
    if (cam.controls && Object.keys(cam.controls).length > 0) {
      try { await applyControl(cam.id, cam.controls) } catch { /* ignore */ }
    }
    if (cam.inference_mode) {
      try { await setInferenceMode(cam.id, cam.inference_mode as never) } catch { /* ignore */ }
    }
  }
}

export async function getRecordingsDir(): Promise<string> {
  const res = await fetch(`${API}/settings/recordings-dir`)
  const data = await res.json()
  return data.data as string
}

export async function setRecordingsDir(path: string): Promise<void> {
  await fetch(`${API}/settings/recordings-dir`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
}

// ---------------------------------------------------------------------------
// Bandwidth
// ---------------------------------------------------------------------------

export async function getBandwidthMatrix(): Promise<BandwidthMatrix> {
  const res = await fetch(`${API}/bandwidth/matrix`)
  return res.json()
}

// ---------------------------------------------------------------------------
// Calibration (IMU angle → camera settings)
// ---------------------------------------------------------------------------

export function useCalibration(cameraId: string) {
  const [profile, setProfile] = useState<CalibrationProfile | null>(null)

  const refresh = useCallback(async () => {
    if (!cameraId) return
    try {
      const res = await fetch(`${API}/camera/${cameraId}/calibration`)
      if (res.ok) {
        const data: CalibrationProfile = await res.json()
        setProfile(data)
      }
    } catch { /* ignore */ }
  }, [cameraId])

  useEffect(() => { refresh() }, [refresh])

  return { profile, refresh }
}

export async function saveCalibrationPoint(
  cameraId: string,
  label: string,
  settings: CameraControlRequest,
): Promise<ApiResponse> {
  const res = await fetch(`${API}/camera/${cameraId}/calibration/point`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label, settings }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({ message: res.statusText }))
    throw new Error(body?.detail ?? body?.message ?? res.statusText)
  }
  return res.json()
}

export async function deleteCalibrationPoint(
  cameraId: string,
  index: number,
): Promise<ApiResponse> {
  const res = await fetch(`${API}/camera/${cameraId}/calibration/point/${index}`, {
    method: 'DELETE',
  })
  return res.json()
}

export async function setCalibrationAutoApply(
  cameraId: string,
  enabled: boolean,
  toleranceDeg?: number,
): Promise<ApiResponse> {
  const res = await fetch(`${API}/camera/${cameraId}/calibration/auto-apply`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled, tolerance_deg: toleranceDeg ?? null }),
  })
  return res.json()
}

export async function setCalibrationInterpolateFocus(
  cameraId: string,
  enabled: boolean,
): Promise<ApiResponse> {
  const res = await fetch(`${API}/camera/${cameraId}/calibration/interpolate-focus`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  })
  return res.json()
}

export async function applyNearestCalibration(cameraId: string): Promise<ApiResponse> {
  const res = await fetch(`${API}/camera/${cameraId}/calibration/apply-nearest`, {
    method: 'POST',
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({ message: res.statusText }))
    throw new Error(body?.detail ?? body?.message ?? res.statusText)
  }
  return res.json()
}

// ---------------------------------------------------------------------------
// Angle targets (closed-loop radial drive correction)
// ---------------------------------------------------------------------------

export function useAngleTargets(camId: string, pollMs = 5000) {
  const [targets, setTargets] = useState<AngleTarget[]>([])

  const refresh = useCallback(async () => {
    if (!camId) return
    try {
      const res = await fetch(`${API}/angle_targets/${camId}`)
      if (res.ok) {
        const data = (await res.json()) as AngleTarget[]
        setTargets(Array.isArray(data) ? data : [])
      }
    } catch { /* ignore */ }
  }, [camId])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, pollMs)
    return () => clearInterval(id)
  }, [refresh, pollMs])

  return { targets, refresh }
}

export async function captureAngleTarget(
  req: CaptureAngleTargetRequest,
): Promise<AngleTarget> {
  const res = await fetch(`${API}/angle_targets/capture`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({ message: res.statusText }))
    throw new Error(body?.detail ?? body?.message ?? res.statusText)
  }
  return res.json()
}

export async function deleteAngleTarget(
  camId: string,
  checkpointName: string,
): Promise<ApiResponse> {
  const res = await fetch(
    `${API}/angle_targets/${camId}/${encodeURIComponent(checkpointName)}`,
    { method: 'DELETE' },
  )
  return res.json()
}

export async function checkBandwidth(
  resolution: string,
  quality: number,
  fps: number,
  numCameras: number,
  stereoMode: string = 'main_only',
): Promise<BandwidthEstimate> {
  const res = await fetch(`${API}/bandwidth/check`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      resolution,
      quality,
      fps,
      num_cameras: numCameras,
      stereo_mode: stereoMode,
    }),
  })
  return res.json()
}
