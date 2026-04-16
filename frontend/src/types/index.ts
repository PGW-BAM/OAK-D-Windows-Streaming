export type InferenceMode = 'none' | 'on_camera' | 'host'
export type RecordingMode = 'video' | 'interval' | 'scheduled'
export type StereoMode = 'main_only' | 'stereo_only' | 'both'

export interface CameraStatus {
  id: string
  name: string
  connected: boolean
  enabled: boolean
  ip: string | null
  fps: number
  latency_ms: number
  recording: boolean
  recording_mode: RecordingMode | null
  inference_mode: InferenceMode
  width: number
  height: number
  stereo_mode: StereoMode
  stream_fps: number
  mjpeg_quality: number
  resolution: string
  flip_180?: boolean
}

export interface BoundingBox {
  x1: number
  y1: number
  x2: number
  y2: number
  confidence: number
  class_id: number
  label: string
}

export interface Detection {
  camera_id: string
  timestamp: number
  boxes: BoundingBox[]
  inference_mode: InferenceMode
}

export interface StorageStatus {
  total_gb: number
  used_gb: number
  free_gb: number
  usage_pct: number
  recordings_gb: number
}

export interface CameraControlRequest {
  auto_exposure?: boolean
  exposure_us?: number
  iso?: number
  auto_focus?: boolean
  manual_focus?: number
  auto_white_balance?: boolean
  white_balance_k?: number
  brightness?: number
  contrast?: number
  saturation?: number
  sharpness?: number
  luma_denoise?: number
  chroma_denoise?: number
}

export interface StreamSettingsRequest {
  fps?: number
  mjpeg_quality?: number
  resolution?: string
  stereo_mode?: StereoMode
  flip_180?: boolean
}

export interface BandwidthEstimate {
  resolution: string
  quality: number
  fps: number
  stereo_mode: string
  num_cameras: number
  per_camera_mbps: number
  total_mbps: number
  budget_mbps: number
  utilization_pct: number
  feasible: boolean
  quality_affects_bandwidth: boolean
}

export interface BandwidthProfile {
  resolution: string
  quality: number
  stereo_mode: string
  num_cameras: number
  max_fps: number
  per_camera_mbps_at_max: number
  total_mbps_at_max: number
}

export interface BandwidthMatrix {
  poe_bandwidth_gbps: number
  usable_bandwidth_mbps: number
  profiles: BandwidthProfile[]
  measured: boolean
}

export interface ApiResponse<T = unknown> {
  ok: boolean
  message: string
  data?: T
}

export interface IMUData {
  has_data: boolean
  roll_deg: number
  pitch_deg: number
}

export interface CalibrationSettings {
  auto_focus: boolean
  manual_focus: number
  auto_exposure: boolean
  exposure_us: number | null
  iso: number | null
  auto_white_balance: boolean
  white_balance_k: number | null
  brightness: number
  contrast: number
  saturation: number
  sharpness: number
  luma_denoise: number
  chroma_denoise: number
}

export interface CalibrationPoint {
  index: number
  label: string
  roll_deg: number
  pitch_deg: number
  settings: CalibrationSettings
  created_at: string
}

export interface CalibrationProfile {
  camera_id: string
  auto_apply: boolean
  tolerance_deg: number
  interpolate_focus: boolean
  points: CalibrationPoint[]
}

export interface AngleTarget {
  checkpoint_name: string
  axis: string
  active_angle: 'roll' | 'pitch'
  target_angle_deg: number
  motor_position: number
  label: string
  created_at: string
}

export interface CaptureAngleTargetRequest {
  camera_id: string
  cam_id: string
  checkpoint_name: string
  active_angle: 'roll' | 'pitch'
  label?: string
}
