import { useCallback, useEffect, useState } from 'react'
import type { BandwidthEstimate, BandwidthMatrix, CameraStatus, Detection, StreamSettingsRequest } from '../types'

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
  mode: 'video' | 'interval',
  intervalSeconds = 5,
  outputDir?: string,
  filenamePrefix?: string,
) {
  const res = await fetch(`${API}/camera/${cameraId}/recording/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode,
      interval_seconds: intervalSeconds,
      output_dir: outputDir || null,
      filename_prefix: filenamePrefix || '',
    }),
  })
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
  mode: 'video' | 'interval',
  intervalSeconds = 5,
  outputDir?: string,
  filenamePrefix?: string,
) {
  const res = await fetch(`${API}/cameras/recording/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode,
      interval_seconds: intervalSeconds,
      output_dir: outputDir || null,
      filename_prefix: filenamePrefix || '',
    }),
  })
  return res.json()
}

export async function stopRecordingAll() {
  const res = await fetch(`${API}/cameras/recording/stop`, { method: 'POST' })
  return res.json()
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

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
