import { useEffect, useState } from 'react'
import {
  applyControl,
  applyControlAll,
  applyNearestCalibration,
  deleteCalibrationPoint,
  getRecordingsDir,
  saveCalibrationPoint,
  setCalibrationAutoApply,
  setCalibrationInterpolateFocus,
  setInferenceMode,
  setRecordingsDir,
  setStreamSettings,
  setStreamSettingsAll,
  getFleetRecordingStatus,
  startRecording,
  startRecordingAll,
  stopRecording,
  stopRecordingAll,
  useCalibration,
  useImuData,
} from '../hooks/useCamera'
import { AngleTargetsPanel } from './AngleTargetsPanel'
import { BandwidthInfo } from './BandwidthInfo'
import type { CalibrationPoint, CameraStatus, InferenceMode, StereoMode } from '../types'

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

  const fpsMax = resolution === '4k' ? 30 : 60
  const [flip180, setFlip180] = useState(camera.flip_180 ?? false)

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

  // Scheduled recording
  const [clipDuration, setClipDuration] = useState(5)   // seconds per clip
  const [clipInterval, setClipInterval] = useState(80)  // total cycle seconds
  const [schedBothCams, setSchedBothCams] = useState(false)

  // Sequential recording
  const [seqGap, setSeqGap] = useState(5)             // inter-camera gap (s)
  const [seqImuThreshold, setSeqImuThreshold] = useState(5) // roll change threshold (°)
  const [seqSettle, setSeqSettle] = useState(3)        // settle after position change (s)
  const [seqActive, setSeqActive] = useState(false)
  useEffect(() => {
    let cancelled = false
    const poll = async () => {
      const status = await getFleetRecordingStatus()
      if (!cancelled) setSeqActive(status.sequential_active)
    }
    poll()
    const id = setInterval(poll, 2000)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  // Calibration
  const { profile: calProfile, refresh: refreshCalibration } = useCalibration(camera.id)
  const imu = useImuData(camera.id)
  const [calOpen, setCalOpen] = useState(false)
  const [calLabel, setCalLabel] = useState('')
  const [calTolerance, setCalTolerance] = useState(5)

  // Keep tolerance slider in sync when profile refreshes
  useEffect(() => {
    if (calProfile) setCalTolerance(calProfile.tolerance_deg)
  }, [calProfile?.tolerance_deg])

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
  const isScheduledActive = currentRecordingMode === 'scheduled'
  const isNonScheduledActive = anyRecording && !isScheduledActive

  useEffect(() => {
    getRecordingsDir().then((dir) => {
      setDefaultDir(dir)
      setOutputDir(dir)
    }).catch(() => {})
  }, [])

  // Sync stream settings when switching cameras
  useEffect(() => {
    const maxFps = camera.resolution === '4k' ? 30 : 60
    setStreamFps(Math.min(camera.stream_fps, maxFps))
    setMjpegQuality(camera.mjpeg_quality)
    setResolution(camera.resolution)
    setStereoMode(camera.stereo_mode)
    setFlip180(camera.flip_180 ?? false)
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
        flip_180: flip180,
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

  // --- Calibration ---
  function buildCurrentControl() {
    // Mirrors sendControl()'s payload shape so saved point reflects what is actually applied.
    return {
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
  }

  async function handleSaveCalPoint() {
    setBusy(true)
    try {
      await saveCalibrationPoint(camera.id, calLabel, buildCurrentControl())
      setCalLabel('')
      await refreshCalibration()
      flash('Calibration point saved')
    } catch (err) {
      flash(`Save failed: ${err instanceof Error ? err.message : 'unknown error'}`)
    } finally { setBusy(false) }
  }

  async function handleApplyCalPoint(p: CalibrationPoint) {
    setBusy(true)
    try {
      // Update sliders to match the point so the UI reflects what's on the device
      setAutoFocus(p.settings.auto_focus)
      setManualFocus(p.settings.manual_focus)
      setAutoExposure(p.settings.auto_exposure)
      if (p.settings.exposure_us != null) setExposureUs(p.settings.exposure_us)
      if (p.settings.iso != null) setIso(p.settings.iso)
      setAutoWb(p.settings.auto_white_balance)
      if (p.settings.white_balance_k != null) setWbK(p.settings.white_balance_k)
      setBrightness(p.settings.brightness)
      setContrast(p.settings.contrast)
      setSaturation(p.settings.saturation)
      setSharpness(p.settings.sharpness)
      setLumaDenoise(p.settings.luma_denoise)
      setChromaDenoise(p.settings.chroma_denoise)
      await applyControl(camera.id, p.settings as unknown as Record<string, unknown>)
      flash(`Applied calibration #${p.index}${p.label ? ` (${p.label})` : ''}`)
    } catch { flash('Error applying calibration') }
    finally { setBusy(false) }
  }

  async function handleDeleteCalPoint(idx: number) {
    if (!confirm(`Delete calibration point #${idx}?`)) return
    setBusy(true)
    try {
      await deleteCalibrationPoint(camera.id, idx)
      await refreshCalibration()
      flash(`Deleted point #${idx}`)
    } catch { flash('Error deleting point') }
    finally { setBusy(false) }
  }

  async function handleToggleAutoApply(enabled: boolean) {
    setBusy(true)
    try {
      await setCalibrationAutoApply(camera.id, enabled, calTolerance)
      await refreshCalibration()
      flash(`Auto-apply ${enabled ? 'on' : 'off'}`)
    } catch { flash('Error toggling auto-apply') }
    finally { setBusy(false) }
  }

  async function handleToggleInterpolateFocus(enabled: boolean) {
    setBusy(true)
    try {
      await setCalibrationInterpolateFocus(camera.id, enabled)
      await refreshCalibration()
      flash(`Focus interpolation ${enabled ? 'on' : 'off'}`)
    } catch { flash('Error toggling focus interpolation') }
    finally { setBusy(false) }
  }

  async function handleToleranceCommit() {
    if (!calProfile) return
    setBusy(true)
    try {
      await setCalibrationAutoApply(camera.id, calProfile.auto_apply, calTolerance)
      await refreshCalibration()
    } catch { /* ignore */ }
    finally { setBusy(false) }
  }

  async function handleApplyNearest() {
    setBusy(true)
    try {
      const res = await applyNearestCalibration(camera.id)
      flash(res.message)
    } catch (err) {
      flash(`Error: ${err instanceof Error ? err.message : 'unknown'}`)
    } finally { setBusy(false) }
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

  async function handleStartScheduled() {
    setBusy(true)
    try {
      const customDir = outputDir !== defaultDir ? outputDir : undefined
      const bothCams = schedBothCams || applyAll
      if (bothCams) {
        await startRecordingAll('scheduled', intervalSecs, customDir, filenamePrefix, clipDuration, clipInterval)
        flash(`Scheduled recording started on all cameras — ${clipDuration}s clips every ${clipInterval}s`)
      } else {
        const result = await startRecording(camera.id, 'scheduled', intervalSecs, customDir, filenamePrefix, clipDuration, clipInterval)
        flash(`Scheduled recording started — ${clipDuration}s clips every ${clipInterval}s → ${result?.data ?? outputDir}`)
      }
      onRefresh()
    } catch (err) { flash(`Error starting scheduled recording: ${err instanceof Error ? err.message : 'unknown error'}`) }
    finally { setBusy(false) }
  }

  async function handleStopScheduled() {
    setBusy(true)
    try {
      const bothCams = schedBothCams || applyAll
      if (bothCams) {
        await stopRecordingAll()
        flash('Scheduled recording stopped on all cameras')
      } else {
        await stopRecording(camera.id)
        flash('Scheduled recording stopped')
      }
      onRefresh()
    } catch { flash('Error stopping scheduled recording') }
    finally { setBusy(false) }
  }

  async function handleStartSequential() {
    setBusy(true)
    try {
      const result = await startRecordingAll(
        'sequential',
        5,
        customDir || undefined,
        filenamePrefix,
        clipDuration,
        80,
        seqGap,
        seqImuThreshold,
        seqSettle,
      )
      setSeqActive(true)
      flash(result?.message ?? `Sequential recording started — ${clipDuration}s clips, ${seqGap}s gap, IMU threshold ${seqImuThreshold}°`)
      onRefresh()
    } catch (err) { flash(`Error starting sequential recording: ${err instanceof Error ? err.message : 'unknown error'}`) }
    finally { setBusy(false) }
  }

  async function handleStopSequential() {
    setBusy(true)
    try {
      await stopRecordingAll()
      setSeqActive(false)
      flash('Sequential recording stopped')
      onRefresh()
    } catch { flash('Error stopping sequential recording') }
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
        <Slider label={`FPS${resolution === '4k' ? ' (max 30 at 4K)' : ''}`} min={1} max={fpsMax} value={streamFps} onChange={setStreamFps} />
        <Slider label="MJPEG Quality" min={10} max={100} step={5} value={mjpegQuality} onChange={setMjpegQuality} />
        <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 12, marginTop: 4 }}>
          <span>Resolution</span>
          <select
            value={resolution}
            onChange={(e) => {
              const res = e.target.value
              setResolution(res)
              if (res === '4k') setStreamFps(fps => Math.min(fps, 30))
            }}
            style={selectStyle}
          >
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
        <label style={{
          display: 'flex', alignItems: 'center', gap: 8,
          fontSize: 12, marginTop: 8, marginBottom: 8, cursor: 'pointer',
          padding: '6px 8px', background: '#1a1a2e',
          border: `1px solid ${flip180 ? '#ffaa44' : '#333'}`, borderRadius: 4,
        }}>
          <input
            type="checkbox"
            checked={flip180}
            onChange={(e) => setFlip180(e.target.checked)}
          />
          <span style={{ color: flip180 ? '#ffaa44' : '#ccc' }}>
            ↻ Rotate stream 180° (ceiling/head mount)
          </span>
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

      {/* ========== CALIBRATION ========== */}
      <section style={{ ...sectionStyle, background: '#15152a', padding: '8px 10px', borderRadius: 6, border: '1px solid #333' }}>
        <div
          onClick={() => setCalOpen((v) => !v)}
          style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', cursor: 'pointer', marginBottom: calOpen ? 10 : 0 }}
        >
          <h4 style={{ ...headingStyle, margin: 0 }}>Calibration (IMU → settings)</h4>
          <span style={{ color: '#aaa', fontSize: 11 }}>
            {calProfile?.points.length ?? 0} pt{(calProfile?.points.length ?? 0) === 1 ? '' : 's'}
            {' '}{calOpen ? '▾' : '▸'}
          </span>
        </div>

        {calOpen && (
          <>
            {/* Live IMU readout */}
            <div style={{
              fontSize: 11, color: imu ? '#8fc' : '#666', background: '#0d0d1a',
              padding: '6px 8px', borderRadius: 4, marginBottom: 8, fontFamily: 'monospace',
            }}>
              IMU:&nbsp;
              {imu
                ? <>Roll {imu.roll_deg.toFixed(1)}°&nbsp; Pitch {imu.pitch_deg.toFixed(1)}°</>
                : <>no data</>}
            </div>

            {/* Label + save */}
            <label style={{ display: 'flex', flexDirection: 'column', gap: 4, fontSize: 12, marginBottom: 6 }}>
              <span>Label (optional)</span>
              <input
                type="text"
                placeholder="e.g. position-A close"
                value={calLabel}
                onChange={(e) => setCalLabel(e.target.value)}
                style={inputStyle}
              />
            </label>
            <button
              onClick={handleSaveCalPoint}
              disabled={busy || !imu}
              title={imu ? 'Save current IMU angle + control sliders as a calibration point' : 'Waiting for IMU data'}
              style={{ ...btnPrimary, background: '#446644', marginBottom: 10 }}
            >
              Save current position
            </button>

            {/* Saved points */}
            {calProfile && calProfile.points.length > 0 && (
              <div style={{ marginBottom: 10 }}>
                <div style={{ fontSize: 11, color: '#99a', marginBottom: 4 }}>Saved points</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                  {calProfile.points.map((p) => (
                    <div
                      key={p.index}
                      style={{
                        display: 'flex', alignItems: 'center', gap: 6,
                        padding: '4px 6px', background: '#0d0d1a',
                        border: '1px solid #2a2a44', borderRadius: 4, fontSize: 11,
                      }}
                    >
                      <span style={{ color: '#88a', width: 20 }}>#{p.index}</span>
                      <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {p.label || <span style={{ color: '#555' }}>(unlabeled)</span>}
                      </span>
                      <span style={{ color: '#aaa', fontFamily: 'monospace', fontSize: 10 }}>
                        R{p.roll_deg >= 0 ? '+' : ''}{p.roll_deg.toFixed(1)}°
                        &nbsp;P{p.pitch_deg >= 0 ? '+' : ''}{p.pitch_deg.toFixed(1)}°
                        &nbsp;F{p.settings.manual_focus}
                      </span>
                      <button
                        onClick={() => handleApplyCalPoint(p)}
                        disabled={busy}
                        style={{
                          padding: '2px 6px', background: '#335', border: '1px solid #557',
                          borderRadius: 3, color: '#cdf', fontSize: 10, cursor: 'pointer',
                        }}
                      >
                        Apply
                      </button>
                      <button
                        onClick={() => handleDeleteCalPoint(p.index)}
                        disabled={busy}
                        title="Delete"
                        style={{
                          padding: '2px 6px', background: '#522', border: '1px solid #744',
                          borderRadius: 3, color: '#faa', fontSize: 10, cursor: 'pointer',
                        }}
                      >
                        ✕
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Auto-apply + tolerance */}
            <label style={{
              display: 'flex', alignItems: 'center', gap: 8, fontSize: 12,
              padding: '6px 8px', background: '#0d0d1a', borderRadius: 4, marginBottom: 8,
              cursor: 'pointer',
            }}>
              <input
                type="checkbox"
                checked={calProfile?.auto_apply ?? false}
                onChange={(e) => handleToggleAutoApply(e.target.checked)}
                disabled={busy}
              />
              <span>Auto-apply nearest point</span>
            </label>

            <label style={{
              display: 'flex', alignItems: 'center', gap: 8, fontSize: 12,
              padding: '6px 8px', background: '#0d0d1a', borderRadius: 4, marginBottom: 8,
              cursor: calProfile?.auto_apply ? 'pointer' : 'not-allowed',
              opacity: calProfile?.auto_apply ? 1 : 0.5,
            }}>
              <input
                type="checkbox"
                checked={calProfile?.interpolate_focus ?? true}
                onChange={(e) => handleToggleInterpolateFocus(e.target.checked)}
                disabled={busy || !calProfile?.auto_apply}
              />
              <span>Interpolate focus between points</span>
            </label>

            <div onMouseUp={handleToleranceCommit} onTouchEnd={handleToleranceCommit}>
              <Slider
                label="Tolerance (°)"
                min={1}
                max={15}
                step={0.5}
                value={calTolerance}
                onChange={setCalTolerance}
              />
            </div>

            <button
              onClick={handleApplyNearest}
              disabled={busy || !imu || !calProfile || calProfile.points.length === 0}
              style={{ ...btnPrimary, background: '#335588', marginTop: 6 }}
            >
              Apply nearest now
            </button>
          </>
        )}
      </section>

      {/* ========== RADIAL ANGLE TEACH ========== */}
      <AngleTargetsPanel
        cameraId={camera.id}
        camId={`cam${Math.max(1, allCameras.findIndex((c) => c.id === camera.id) + 1)}`}
        imu={imu}
        onFlash={setMsg}
      />

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

        {/* Recording status (video / interval) */}
        {isNonScheduledActive && (
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
          {!isNonScheduledActive && (
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
          {/* Stop button (non-scheduled) */}
          {isNonScheduledActive && (
            <button
              onClick={handleStopRecording}
              disabled={busy}
              style={{ ...btnPrimary, background: '#883333' }}
            >
              Stop recording{applyAll ? ' (all)' : ''}
            </button>
          )}
        </div>

        {/* ---- Scheduled recording sub-section ---- */}
        <div style={{
          marginTop: 14, padding: '10px 10px 8px', background: '#111a2e',
          border: `1px solid ${isScheduledActive ? '#44aaff' : '#2a3a55'}`, borderRadius: 6,
        }}>
          <div style={{ fontSize: 12, color: '#88bbff', fontWeight: 700, marginBottom: 8 }}>
            Scheduled clips
          </div>

          <Slider
            label={`Clip duration (s)`}
            min={1}
            max={300}
            step={1}
            value={clipDuration}
            onChange={(v) => setClipDuration(Math.min(v, clipInterval - 1))}
          />
          <Slider
            label={`Interval / cycle (s)`}
            min={clipDuration + 1}
            max={3600}
            step={1}
            value={clipInterval}
            onChange={setClipInterval}
          />
          <div style={{ fontSize: 10, color: '#557', marginBottom: 8 }}>
            Idle between clips: {clipInterval - clipDuration}s
            &nbsp;·&nbsp;~{Math.round(clipDuration / clipInterval * 100)}% duty cycle
          </div>

          {/* Both-cameras toggle */}
          <label style={{
            display: 'flex', alignItems: 'center', gap: 8,
            fontSize: 12, cursor: 'pointer', marginBottom: 8,
            padding: '5px 7px', background: '#0d1525',
            border: `1px solid ${schedBothCams ? '#44aaff' : '#2a3a55'}`, borderRadius: 4,
          }}>
            <input
              type="checkbox"
              checked={schedBothCams || applyAll}
              disabled={applyAll}
              onChange={(e) => setSchedBothCams(e.target.checked)}
              style={{ accentColor: '#44aaff' }}
            />
            <span style={{ color: (schedBothCams || applyAll) ? '#88ccff' : '#999' }}>
              Apply to both cameras simultaneously
            </span>
          </label>

          {/* Scheduled active status */}
          {isScheduledActive && (
            <div style={{
              background: '#0a1e3a', border: '1px solid #2255aa', borderRadius: 4,
              padding: '5px 8px', fontSize: 11, color: '#66aaff', marginBottom: 8,
            }}>
              Scheduled recording active
              {(schedBothCams || applyAll) && ` on ${allCameras.filter(c => c.recording).length} camera(s)`}
              &nbsp;— {clipDuration}s clips every {clipInterval}s
            </div>
          )}

          {!isScheduledActive ? (
            <button
              onClick={handleStartScheduled}
              disabled={busy}
              style={{ ...btnPrimary, background: '#1e4488' }}
            >
              Start scheduled
              {(schedBothCams || applyAll) ? ' (both cams)' : ''}
            </button>
          ) : (
            <button
              onClick={handleStopScheduled}
              disabled={busy}
              style={{ ...btnPrimary, background: '#883333' }}
            >
              Stop scheduled
              {(schedBothCams || applyAll) ? ' (both cams)' : ''}
            </button>
          )}
        </div>

        {/* ---- Sequential interleaved recording sub-section ---- */}
        <div style={{
          marginTop: 10, padding: '10px 10px 8px', background: '#111a2e',
          border: `1px solid ${seqActive ? '#44ffaa' : '#2a3a55'}`, borderRadius: 6,
        }}>
          <div style={{ fontSize: 12, color: '#88ffcc', fontWeight: 700, marginBottom: 8 }}>
            Sequential (cam1 → cam2, IMU-gated)
          </div>
          <div style={{ fontSize: 10, color: '#557', marginBottom: 8, lineHeight: 1.4 }}>
            Cameras record one at a time. After cam2 finishes, waits for the rig to move
            to a new position (IMU), then repeats from cam1.
          </div>

          <Slider label={`Clip duration (s)`} min={1} max={300} step={1}
            value={clipDuration} onChange={setClipDuration} />

          <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 12, marginBottom: 6 }}>
            <span style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span>Inter-camera gap (s)</span>
              <span style={{ color: '#aaa' }}>{seqGap}</span>
            </span>
            <input type="number" min={0} max={60} step={1} value={seqGap}
              onChange={(e) => setSeqGap(Math.max(0, Number(e.target.value)))}
              style={{ background: '#0d1525', border: '1px solid #334', borderRadius: 3,
                color: '#eee', padding: '3px 6px', fontSize: 12 }} />
          </label>

          <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 12, marginBottom: 6 }}>
            <span style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span>IMU change threshold (°)</span>
              <span style={{ color: '#aaa' }}>{seqImuThreshold}</span>
            </span>
            <input type="number" min={1} max={90} step={0.5} value={seqImuThreshold}
              onChange={(e) => setSeqImuThreshold(Math.max(0.5, Number(e.target.value)))}
              style={{ background: '#0d1525', border: '1px solid #334', borderRadius: 3,
                color: '#eee', padding: '3px 6px', fontSize: 12 }} />
          </label>

          <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 12, marginBottom: 8 }}>
            <span style={{ display: 'flex', justifyContent: 'space-between' }}>
              <span>Settle after position change (s)</span>
              <span style={{ color: '#aaa' }}>{seqSettle}</span>
            </span>
            <input type="number" min={0} max={30} step={1} value={seqSettle}
              onChange={(e) => setSeqSettle(Math.max(0, Number(e.target.value)))}
              style={{ background: '#0d1525', border: '1px solid #334', borderRadius: 3,
                color: '#eee', padding: '3px 6px', fontSize: 12 }} />
          </label>

          {seqActive && (
            <div style={{
              background: '#0a2e1e', border: '1px solid #227755', borderRadius: 4,
              padding: '5px 8px', fontSize: 11, color: '#66ffaa', marginBottom: 8,
            }}>
              Sequential recording active — {clipDuration}s clips, {seqGap}s gap, IMU ±{seqImuThreshold}°
            </div>
          )}

          {!seqActive ? (
            <button onClick={handleStartSequential} disabled={busy}
              style={{ ...btnPrimary, background: '#1e6644' }}>
              Start sequential (all cams)
            </button>
          ) : (
            <button onClick={handleStopSequential} disabled={busy}
              style={{ ...btnPrimary, background: '#883333' }}>
              Stop sequential
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
