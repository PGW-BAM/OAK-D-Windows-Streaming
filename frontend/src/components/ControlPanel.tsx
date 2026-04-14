import { useEffect, useState } from 'react'
import {
  applyControl,
  applyControlAll,
  getRecordingsDir,
  setInferenceMode,
  setRecordingsDir,
  setStreamSettings,
  setStreamSettingsAll,
  startRecording,
  startRecordingAll,
  stopRecording,
  stopRecordingAll,
} from '../hooks/useCamera'
import { BandwidthInfo } from './BandwidthInfo'
import type { CameraStatus, InferenceMode, StereoMode } from '../types'

interface Props {
  camera: CameraStatus
  allCameras: CameraStatus[]
  onClose: () => void
  onRefresh: () => void
}

function Slider({
  label,
  min,
  max,
  step,
  value,
  onChange,
}: {
  label: string
  min: number
  max: number
  step?: number
  value: number
  onChange: (v: number) => void
}) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 12 }}>
      <span style={{ display: 'flex', justifyContent: 'space-between' }}>
        <span>{label}</span>
        <span>{value}</span>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step ?? 1}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ width: '100%' }}
      />
    </label>
  )
}

const sectionStyle: React.CSSProperties = { marginBottom: 16 }
const headingStyle: React.CSSProperties = { margin: '0 0 8px', color: '#88f', fontSize: 13 }
const selectStyle: React.CSSProperties = {
  width: '100%', marginBottom: 8, padding: '6px',
  background: '#111', color: '#eee', border: '1px solid #333', borderRadius: 4,
}
const inputStyle: React.CSSProperties = {
  width: '100%', padding: '6px', background: '#111', color: '#eee',
  border: '1px solid #333', borderRadius: 4, fontFamily: 'monospace',
  fontSize: 11, boxSizing: 'border-box',
}
const btnPrimary: React.CSSProperties = {
  width: '100%', padding: '8px', border: 'none', borderRadius: 4,
  color: '#fff', cursor: 'pointer', fontSize: 12, fontFamily: 'monospace',
}

export function ControlPanel({ camera, allCameras, onClose, onRefresh }: Props) {
  // "Apply to all" toggle
  const [applyAll, setApplyAll] = useState(false)

  // Stream settings
  const [streamFps, setStreamFps] = useState(camera.stream_fps)
  const [mjpegQuality, setMjpegQuality] = useState(camera.mjpeg_quality)
  const [resolution, setResolution] = useState(camera.resolution)
  const [stereoMode, setStereoMode] = useState<StereoMode>(camera.stereo_mode)

  // Camera control
  const [autoExposure, setAutoExposure] = useState(true)
  const [exposureUs, setExposureUs] = useState(10000)
  const [iso, setIso] = useState(400)
  const [autoFocus, setAutoFocus] = useState(true)
  const [manualFocus, setManualFocus] = useState(128)
  const [autoWb, setAutoWb] = useState(true)
  const [wbK, setWbK] = useState(5500)
  const [brightness, setBrightness] = useState(0)
  const [contrast, setContrast] = useState(0)
  const [saturation, setSaturation] = useState(0)
  const [sharpness, setSharpness] = useState(0)
  const [lumaDenoise, setLumaDenoise] = useState(0)
  const [chromaDenoise, setChromaDenoise] = useState(0)

  // Inference
  const [inferenceMode, setInferMode] = useState<InferenceMode>(camera.inference_mode)
  const [modelPath, setModelPath] = useState('')

  // Recording
  const [intervalSecs, setIntervalSecs] = useState(5)
  const [outputDir, setOutputDir] = useState('')
  const [defaultDir, setDefaultDir] = useState('')
  const [filenamePrefix, setFilenamePrefix] = useState('')

  // UI state
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')

  // Which cameras are affected by "apply all"?
  const anyRecording = applyAll
    ? allCameras.some((c) => c.recording)
    : camera.recording
  const currentRecordingMode = applyAll
    ? allCameras.find((c) => c.recording)?.recording_mode ?? null
    : camera.recording_mode

  useEffect(() => {
    getRecordingsDir().then((dir) => {
      setDefaultDir(dir)
      setOutputDir(dir)
    }).catch(() => {})
  }, [])

  // Sync stream settings when switching cameras
  useEffect(() => {
    setStreamFps(camera.stream_fps)
    setMjpegQuality(camera.mjpeg_quality)
    setResolution(camera.resolution)
    setStereoMode(camera.stereo_mode)
    setInferMode(camera.inference_mode)
  }, [camera.id])

  const flash = (m: string) => { setMsg(m); setTimeout(() => setMsg(''), 3000) }

  // --- Stream settings ---
  async function sendStreamSettings() {
    setBusy(true)
    try {
      const payload = {
        fps: streamFps,
        mjpeg_quality: mjpegQuality,
        resolution,
        stereo_mode: stereoMode,
      }
      if (applyAll) {
        await setStreamSettingsAll(payload)
        flash('Stream settings applied to all cameras')
      } else {
        await setStreamSettings(camera.id, payload)
        flash('Stream settings applied')
      }
      onRefresh()
    } catch { flash('Error applying stream settings') }
    finally { setBusy(false) }
  }

  // --- Camera control ---
  async function sendControl() {
    setBusy(true)
    try {
      const payload = {
        auto_exposure: autoExposure,
        ...(autoExposure ? {} : { exposure_us: exposureUs, iso }),
        auto_focus: autoFocus,
        ...(autoFocus ? {} : { manual_focus: manualFocus }),
        auto_white_balance: autoWb,
        ...(autoWb ? {} : { white_balance_k: wbK }),
        brightness,
        contrast,
        saturation,
        sharpness,
        luma_denoise: lumaDenoise,
        chroma_denoise: chromaDenoise,
      }
      if (applyAll) {
        await applyControlAll(payload)
        flash('Control applied to all cameras')
      } else {
        await applyControl(camera.id, payload)
        flash('Control applied')
      }
    } catch { flash('Error applying control') }
    finally { setBusy(false) }
  }

  // --- Inference ---
  async function applyInference() {
    setBusy(true)
    try {
      await setInferenceMode(camera.id, inferenceMode, modelPath || undefined)
      onRefresh()
      flash(`Inference: ${inferenceMode}`)
    } catch { flash('Error setting inference mode') }
    finally { setBusy(false) }
  }

  // --- Recording ---
  async function saveOutputDir() {
    setBusy(true)
    try {
      await setRecordingsDir(outputDir)
      setDefaultDir(outputDir)
      flash(`Save directory: ${outputDir}`)
    } catch { flash('Error setting directory') }
    finally { setBusy(false) }
  }

  async function handleStartRecording(mode: 'video' | 'interval') {
    setBusy(true)
    try {
      const customDir = outputDir !== defaultDir ? outputDir : undefined
      if (applyAll) {
        await startRecordingAll(mode, intervalSecs, customDir, filenamePrefix)
        flash(`${mode === 'video' ? 'Video' : 'Interval'} recording started on all cameras`)
      } else {
        const result = await startRecording(camera.id, mode, intervalSecs, customDir, filenamePrefix)
        const savePath = result?.data
        flash(`${mode === 'video' ? 'Video' : 'Interval'} recording started — saving to: ${savePath ?? outputDir}`)
      }
      onRefresh()
    } catch (err) { flash(`Error starting recording: ${err instanceof Error ? err.message : 'unknown error'}`) }
    finally { setBusy(false) }
  }

  async function handleStopRecording() {
    setBusy(true)
    try {
      if (applyAll) {
        await stopRecordingAll()
        flash('All recordings stopped')
      } else {
        await stopRecording(camera.id)
        flash('Recording stopped')
      }
      onRefresh()
    } catch { flash('Error stopping recording') }
    finally { setBusy(false) }
  }

  const panelTitle = applyAll
    ? `All cameras (${allCameras.length})`
    : camera.name

  return (
    <div
      style={{
        position: 'fixed',
        top: 0,
        right: 0,
        bottom: 0,
        width: 340,
        background: '#1a1a2e',
        borderLeft: '1px solid #333',
        padding: 16,
        overflowY: 'auto',
        zIndex: 1000,
        color: '#eee',
        fontFamily: 'monospace',
      }}
    >
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 12 }}>
        <strong style={{ fontSize: 14 }}>{panelTitle}</strong>
        <button onClick={onClose} style={{ background: 'none', border: 'none', color: '#aaa', cursor: 'pointer', fontSize: 18 }}>✕</button>
      </div>

      {/* Flash message */}
      {msg && (
        <div style={{ background: '#2a2a4e', padding: '6px 10px', borderRadius: 4, marginBottom: 12, fontSize: 12 }}>
          {msg}
        </div>
      )}

      {/* ========== APPLY TO ALL TOGGLE ========== */}
      <section style={{ ...sectionStyle, background: '#1e1e3a', padding: '8px 10px', borderRadius: 6, border: applyAll ? '1px solid #5577ff' : '1px solid #333' }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={applyAll}
            onChange={(e) => setApplyAll(e.target.checked)}
            style={{ accentColor: '#5577ff' }}
          />
          <span style={{ fontWeight: 700, color: applyAll ? '#aaccff' : '#999' }}>
            Apply to all cameras ({allCameras.length})
          </span>
        </label>
        {applyAll && (
          <div style={{ fontSize: 11, color: '#777', marginTop: 4, paddingLeft: 24 }}>
            Settings, controls, and recording will be applied to every connected camera.
          </div>
        )}
      </section>

      {/* ========== STREAM SETTINGS ========== */}
      <section style={sectionStyle}>
        <h4 style={headingStyle}>Stream Settings</h4>
        <Slider label="FPS" min={1} max={60} value={streamFps} onChange={setStreamFps} />
        <Slider label="MJPEG Quality" min={10} max={100} step={5} value={mjpegQuality} onChange={setMjpegQuality} />
        <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 12, marginTop: 4 }}>
          <span>Resolution</span>
          <select value={resolution} onChange={(e) => setResolution(e.target.value)} style={selectStyle}>
            <option value="4k">4K (3840x2160)</option>
            <option value="1080p">1080p (1920x1080)</option>
            <option value="720p">720p (1280x720)</option>
            <option value="480p">480p (640x480)</option>
          </select>
        </label>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 12 }}>
          <span>Stereo Cameras</span>
          <select value={stereoMode} onChange={(e) => setStereoMode(e.target.value as StereoMode)} style={selectStyle}>
            <option value="main_only">Main camera only (RGB)</option>
            <option value="both">Main + Stereo (L/R)</option>
            <option value="stereo_only">Stereo only (L/R)</option>
          </select>
        </label>
        {/* Bandwidth feasibility indicator */}
        <BandwidthInfo
          resolution={resolution}
          quality={mjpegQuality}
          fps={streamFps}
          numCameras={applyAll ? allCameras.filter(c => c.enabled).length : (camera.enabled ? 1 : 0)}
          totalCameras={allCameras.length}
          disabledCameras={allCameras.filter(c => !c.enabled).length}
          stereoMode={stereoMode}
        />

        <div style={{ fontSize: 11, color: '#666', marginBottom: 6 }}>
          Changing these settings will restart the camera pipeline.
        </div>
        <button
          onClick={sendStreamSettings}
          disabled={busy}
          style={{ ...btnPrimary, background: '#4455cc' }}
        >
          Apply stream settings{applyAll ? ' to all' : ''}
        </button>
      </section>

      {/* ========== EXPOSURE ========== */}
      <section style={sectionStyle}>
        <h4 style={headingStyle}>Exposure</h4>
        <label style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
          <input type="checkbox" checked={autoExposure} onChange={(e) => setAutoExposure(e.target.checked)} />
          Auto exposure
        </label>
        {!autoExposure && (
          <>
            <Slider label="Exposure (us)" min={1} max={33000} value={exposureUs} onChange={setExposureUs} />
            <Slider label="ISO" min={100} max={1600} value={iso} onChange={setIso} />
          </>
        )}
      </section>

      {/* ========== FOCUS ========== */}
      <section style={sectionStyle}>
        <h4 style={headingStyle}>Focus</h4>
        <label style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
          <input type="checkbox" checked={autoFocus} onChange={(e) => setAutoFocus(e.target.checked)} />
          Auto focus
        </label>
        {!autoFocus && (
          <Slider label="Manual focus" min={0} max={255} value={manualFocus} onChange={setManualFocus} />
        )}
      </section>

      {/* ========== WHITE BALANCE ========== */}
      <section style={sectionStyle}>
        <h4 style={headingStyle}>White Balance</h4>
        <label style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
          <input type="checkbox" checked={autoWb} onChange={(e) => setAutoWb(e.target.checked)} />
          Auto white balance
        </label>
        {!autoWb && (
          <Slider label="Color temp (K)" min={1000} max={12000} value={wbK} onChange={setWbK} />
        )}
      </section>

      {/* ========== IMAGE QUALITY ========== */}
      <section style={sectionStyle}>
        <h4 style={headingStyle}>Image Quality</h4>
        <Slider label="Brightness" min={-10} max={10} value={brightness} onChange={setBrightness} />
        <Slider label="Contrast" min={-10} max={10} value={contrast} onChange={setContrast} />
        <Slider label="Saturation" min={-10} max={10} value={saturation} onChange={setSaturation} />
        <Slider label="Sharpness" min={0} max={4} value={sharpness} onChange={setSharpness} />
        <Slider label="Luma denoise" min={0} max={4} value={lumaDenoise} onChange={setLumaDenoise} />
        <Slider label="Chroma denoise" min={0} max={4} value={chromaDenoise} onChange={setChromaDenoise} />
      </section>

      <button
        onClick={sendControl}
        disabled={busy}
        style={{ ...btnPrimary, background: '#4455cc', marginBottom: 16 }}
      >
        Apply camera control{applyAll ? ' to all' : ''}
      </button>

      {/* ========== RECORDING ========== */}
      <section style={sectionStyle}>
        <h4 style={headingStyle}>Recording</h4>

        <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12, marginBottom: 8 }}>
          <span>Save directory</span>
          <div style={{ display: 'flex', gap: 4 }}>
            <input
              type="text"
              value={outputDir}
              onChange={(e) => setOutputDir(e.target.value)}
              style={{ ...inputStyle, flex: 1 }}
            />
            <button
              onClick={saveOutputDir}
              disabled={busy || outputDir === defaultDir}
              title="Set as default recordings directory"
              style={{
                padding: '6px 10px',
                background: outputDir !== defaultDir ? '#446' : '#222',
                border: '1px solid #444', borderRadius: 4,
                color: '#cdf', cursor: outputDir !== defaultDir ? 'pointer' : 'default',
                fontSize: 11,
              }}
            >
              Set
            </button>
          </div>
        </label>

        <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12, marginBottom: 8 }}>
          <span>Filename prefix (optional)</span>
          <input
            type="text"
            placeholder="e.g. experiment_001"
            value={filenamePrefix}
            onChange={(e) => setFilenamePrefix(e.target.value)}
            style={inputStyle}
          />
        </label>

        <Slider label="Interval (s)" min={1} max={60} value={intervalSecs} onChange={setIntervalSecs} />

        {/* Recording status */}
        {anyRecording && (
          <div style={{
            background: '#331a1a', border: '1px solid #663333', borderRadius: 4,
            padding: '6px 10px', fontSize: 11, color: '#ff8888', marginTop: 8, marginBottom: 8,
          }}>
            Recording active ({currentRecordingMode})
            {applyAll && ` on ${allCameras.filter(c => c.recording).length} camera(s)`}
          </div>
        )}

        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 8 }}>
          {/* Start buttons */}
          {!anyRecording && (
            <div style={{ display: 'flex', gap: 6 }}>
              <button
                onClick={() => handleStartRecording('video')}
                disabled={busy}
                style={{ ...btnPrimary, flex: 1, background: '#335588' }}
              >
                Start video{applyAll ? ' (all)' : ''}
              </button>
              <button
                onClick={() => handleStartRecording('interval')}
                disabled={busy}
                style={{ ...btnPrimary, flex: 1, background: '#335588' }}
              >
                Start interval{applyAll ? ' (all)' : ''}
              </button>
            </div>
          )}
          {/* Stop button */}
          {anyRecording && (
            <button
              onClick={handleStopRecording}
              disabled={busy}
              style={{ ...btnPrimary, background: '#883333' }}
            >
              Stop recording{applyAll ? ' (all)' : ''}
            </button>
          )}
        </div>

        {stereoMode !== 'main_only' && (
          <div style={{ fontSize: 11, color: '#88aa88', marginTop: 6 }}>
            Stereo frames (L/R) will be captured alongside recordings.
          </div>
        )}
      </section>

      {/* ========== AI INFERENCE ========== */}
      <section style={sectionStyle}>
        <h4 style={headingStyle}>AI Inference</h4>
        <select
          value={inferenceMode}
          onChange={(e) => setInferMode(e.target.value as InferenceMode)}
          style={selectStyle}
        >
          <option value="none">Off</option>
          <option value="on_camera">On-camera (SNPE)</option>
          <option value="host">Host GPU (Ultralytics)</option>
        </select>
        {inferenceMode === 'host' && (
          <input
            type="text"
            placeholder="Model path (blank = yolov8n.pt)"
            value={modelPath}
            onChange={(e) => setModelPath(e.target.value)}
            style={{ ...inputStyle, marginBottom: 8 }}
          />
        )}
        <button
          onClick={applyInference}
          disabled={busy}
          style={{ ...btnPrimary, background: '#336644' }}
        >
          Set inference mode
        </button>
        <div style={{ fontSize: 11, color: '#666', marginTop: 4 }}>
          Inference is set per-camera (not affected by "apply to all").
        </div>
      </section>
    </div>
  )
}
