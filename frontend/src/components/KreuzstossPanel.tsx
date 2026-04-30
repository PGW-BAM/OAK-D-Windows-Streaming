import { useEffect, useState } from 'react'
import { useKreuzstoss } from '../contexts/KreuzstossContext'
import {
  getKreuzstossConfig,
  setKreuzstossConfig,
  startKreuzstoss,
  stopKreuzstoss,
  useKreuzstossStatus,
} from '../hooks/useCamera'
import type { KreuzstossMode, KreuzstossPhase, KreuzstossStatus } from '../hooks/useCamera'

const MODE_INFO: Record<KreuzstossMode, { title: string; description: string }> = {
  full: {
    title: 'Kreuzstoss Programm',
    description:
      'Loops a fixed dual-camera recording sequence. Each cycle: ' +
      '5 s @ 4K/29 fps + JPEG snapshot + 5 s @ 1080p/59 fps on cam1, ' +
      'then the same on cam2. Manual focus / exposure / white balance ' +
      'stay untouched. Resolution & FPS are restored at the end.',
  },
  simple: {
    title: 'Kreuzstoss Simple',
    description:
      'Bandwidth-safe variant for fast motion. Each cycle on both ' +
      'cameras: 5 s @ 1080p/59 fps video (smooth — fits the GbE link), ' +
      'then a single 4K JPEG snapshot. Avoids the 4K-video stutter that ' +
      'comes from saturating the PoE uplink. Loops until disk full or stop.',
  },
}

const overlayStyle: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  background: 'rgba(0, 0, 0, 0.7)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  zIndex: 1000,
}

const cardStyle: React.CSSProperties = {
  background: '#11111a',
  color: '#eee',
  border: '1px solid #444',
  borderRadius: 10,
  padding: 24,
  minWidth: 480,
  maxWidth: 720,
  fontFamily: 'monospace',
}

const inputStyle: React.CSSProperties = {
  background: '#000',
  color: '#eee',
  border: '1px solid #444',
  borderRadius: 4,
  padding: '6px 10px',
  fontFamily: 'monospace',
  fontSize: 14,
  width: '100%',
  boxSizing: 'border-box',
}

const buttonStyle: React.CSSProperties = {
  padding: '8px 16px',
  border: '1px solid #555',
  borderRadius: 4,
  background: '#222',
  color: '#eee',
  fontFamily: 'monospace',
  cursor: 'pointer',
  fontSize: 13,
}

const phaseColors: Record<KreuzstossPhase, string> = {
  idle: '#666',
  cam1_active: '#3399ff',
  cam2_active: '#9b59b6',
  interval: '#888',
  restoring: '#e67e22',
  error: '#cc2222',
  done: '#44dd66',
  stopped: '#999',
}

interface Props {
  cameraCount: number
}

export function KreuzstossPanel({ cameraCount }: Props) {
  const { panelOpen, panelMode, closePanel, setRunning } = useKreuzstoss()
  const status = useKreuzstossStatus(500, panelOpen)

  // Track running state into context so other components can disable controls.
  useEffect(() => {
    if (status) setRunning(status.running)
  }, [status?.running, setRunning, status])

  if (!panelOpen) return null

  // When a run is active, show its mode (status.mode) — could be the
  // mode the user opened the panel in, or the mode someone else started.
  if (status?.running) {
    return <ProgressView status={status} onClose={closePanel} />
  }

  return (
    <ConfigureView
      cameraCount={cameraCount}
      lastStatus={status}
      mode={panelMode}
      onClose={closePanel}
    />
  )
}

interface ConfigureProps {
  cameraCount: number
  lastStatus: KreuzstossStatus | null
  mode: KreuzstossMode
  onClose: () => void
}

function ConfigureView({ cameraCount, lastStatus, mode, onClose }: ConfigureProps) {
  const [saveDir, setSaveDir] = useState('')
  const [interval, setInterval] = useState('5')
  const [minInterval, setMinInterval] = useState(5)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    getKreuzstossConfig().then(cfg => {
      if (cancelled) return
      setSaveDir(cfg.save_dir)
      setInterval(String(cfg.interval_seconds))
      setMinInterval(cfg.min_interval_seconds)
      setLoaded(true)
    })
    return () => {
      cancelled = true
    }
  }, [])

  const intervalNum = Number(interval)
  const intervalValid = !isNaN(intervalNum) && intervalNum >= minInterval
  const enoughCameras = cameraCount >= 2

  const onSaveConfig = async () => {
    setBusy(true)
    setError(null)
    try {
      await setKreuzstossConfig({
        save_dir: saveDir,
        interval_seconds: intervalNum,
      })
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const onStart = async () => {
    if (!enoughCameras || !intervalValid) return
    setBusy(true)
    setError(null)
    try {
      await setKreuzstossConfig({
        save_dir: saveDir,
        interval_seconds: intervalNum,
      })
      await startKreuzstoss(saveDir, intervalNum, mode)
      // Keep panel open so the progress overlay shows up via status polling.
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const lastError = lastStatus?.error
  const lastPhase = lastStatus?.phase

  const info = MODE_INFO[mode]
  const titleColor = mode === 'simple' ? '#4cf' : '#fc4'

  return (
    <div style={overlayStyle} onClick={onClose}>
      <div style={cardStyle} onClick={e => e.stopPropagation()}>
        <h2 style={{ margin: 0, marginBottom: 12, color: titleColor }}>
          {info.title}
        </h2>
        <p style={{ marginTop: 0, color: '#bbb', fontSize: 13 }}>
          {info.description}
        </p>

        <div style={{ marginBottom: 14 }}>
          <label style={{ display: 'block', marginBottom: 4, fontSize: 12, color: '#aaa' }}>
            Cameras detected
          </label>
          <div style={{ color: enoughCameras ? '#44dd66' : '#cc2222' }}>
            {cameraCount} {cameraCount === 1 ? 'camera' : 'cameras'}
            {!enoughCameras && ' — connect 2 cameras to run'}
          </div>
        </div>

        <div style={{ marginBottom: 14 }}>
          <label style={{ display: 'block', marginBottom: 4, fontSize: 12, color: '#aaa' }}>
            Save folder (fallback to project recordings if unwritable)
          </label>
          <input
            value={saveDir}
            onChange={e => setSaveDir(e.target.value)}
            placeholder="D:\Kreuzstöße\P02"
            style={inputStyle}
            disabled={busy}
          />
          <div style={{ fontSize: 11, color: '#888', marginTop: 4 }}>
            Folder name becomes the filename prefix
            (e.g. <code>P02_cam1_4K_29fps_…</code>).
          </div>
        </div>

        <div style={{ marginBottom: 16 }}>
          <label style={{ display: 'block', marginBottom: 4, fontSize: 12, color: '#aaa' }}>
            Interval between cycles (seconds)
          </label>
          <input
            type="number"
            min={minInterval}
            step="1"
            value={interval}
            onChange={e => setInterval(e.target.value)}
            style={inputStyle}
            disabled={busy}
          />
          <div style={{ fontSize: 11, color: '#888', marginTop: 4 }}>
            Minimum {minInterval} s — covers pipeline rebuild + settle time.
            The loop runs forever; stop it with the button on the progress
            overlay or it will stop automatically when free disk space is below
            2 GB.
          </div>
        </div>

        {lastError && lastPhase !== 'idle' && (
          <div
            style={{
              padding: 10,
              border: '1px solid #cc2222',
              background: '#3a0a0a',
              color: '#ffaaaa',
              borderRadius: 4,
              marginBottom: 14,
              fontSize: 12,
            }}
          >
            Previous run ({lastPhase}): {lastError}
          </div>
        )}

        {error && (
          <div
            style={{
              padding: 10,
              border: '1px solid #cc2222',
              background: '#3a0a0a',
              color: '#ffaaaa',
              borderRadius: 4,
              marginBottom: 14,
              fontSize: 12,
            }}
          >
            {error}
          </div>
        )}

        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            style={buttonStyle}
          >
            Close
          </button>
          <button
            type="button"
            onClick={onSaveConfig}
            disabled={!loaded || busy}
            style={buttonStyle}
          >
            Save defaults
          </button>
          <button
            type="button"
            onClick={onStart}
            disabled={!loaded || busy || !enoughCameras || !intervalValid}
            style={{
              ...buttonStyle,
              background: '#0a6622',
              borderColor: '#44dd66',
              color: '#fff',
              fontWeight: 700,
              minWidth: 120,
            }}
          >
            {busy ? 'Starting…' : 'START'}
          </button>
        </div>
      </div>
    </div>
  )
}

interface ProgressProps {
  status: KreuzstossStatus
  onClose: () => void
}

function ProgressView({ status, onClose }: ProgressProps) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const onStop = async () => {
    setBusy(true)
    setError(null)
    try {
      await stopKreuzstoss()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const pct = status.total_steps > 0
    ? Math.min(100, (status.step_index / status.total_steps) * 100)
    : 0
  const phaseColor = phaseColors[status.phase] ?? '#888'

  return (
    <div style={overlayStyle}>
      <div
        style={{ ...cardStyle, minWidth: 560 }}
        onClick={e => e.stopPropagation()}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
          <h2 style={{ margin: 0, color: status.mode === 'simple' ? '#4cf' : '#fc4' }}>
            {status.mode === 'simple' ? 'Kreuzstoss Simple läuft' : 'Kreuzstoss läuft'}
          </h2>
          <span
            style={{
              padding: '3px 10px',
              borderRadius: 999,
              background: phaseColor,
              color: '#fff',
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: 1,
              textTransform: 'uppercase',
            }}
          >
            {status.phase}
          </span>
        </div>

        <div style={{ marginBottom: 8, color: '#bbb', fontSize: 13 }}>
          Cycle <strong style={{ color: '#fff' }}>{status.cycle_index}</strong>
          {' '}— Step{' '}
          <strong style={{ color: '#fff' }}>
            {status.step_index}/{status.total_steps}
          </strong>
        </div>

        <div style={{ fontSize: 18, fontWeight: 700, marginBottom: 12 }}>
          {status.current_step}
        </div>

        <div
          style={{
            height: 10,
            background: '#222',
            borderRadius: 4,
            overflow: 'hidden',
            border: '1px solid #333',
            marginBottom: 14,
          }}
        >
          <div
            style={{
              height: '100%',
              width: `${pct}%`,
              background: phaseColor,
              transition: 'width 200ms ease',
            }}
          />
        </div>

        {status.phase === 'interval' && status.interval_remaining_s != null && (
          <div style={{ fontSize: 13, color: '#bbb', marginBottom: 10 }}>
            Next cycle in <strong style={{ color: '#fff' }}>
              {status.interval_remaining_s.toFixed(1)} s
            </strong>
          </div>
        )}

        <div
          style={{
            display: 'grid',
            gridTemplateColumns: '1fr 1fr',
            gap: 10,
            fontSize: 12,
            color: '#aaa',
            marginBottom: 16,
          }}
        >
          <div>
            <div style={{ color: '#888' }}>Save dir</div>
            <div style={{ color: '#eee', wordBreak: 'break-all' }}>
              {status.save_dir}
            </div>
          </div>
          <div>
            <div style={{ color: '#888' }}>Files written</div>
            <div style={{ color: '#eee' }}>{status.artifacts_total}</div>
          </div>
          <div>
            <div style={{ color: '#888' }}>Free space</div>
            <div
              style={{
                color:
                  status.free_space_gb != null && status.free_space_gb < 4
                    ? '#cc2222'
                    : '#eee',
              }}
            >
              {status.free_space_gb != null
                ? `${status.free_space_gb.toFixed(1)} GB`
                : '—'}
            </div>
          </div>
          <div>
            <div style={{ color: '#888' }}>Last file</div>
            <div style={{ color: '#eee', wordBreak: 'break-all' }}>
              {status.last_artifact ?? '—'}
            </div>
          </div>
        </div>

        {status.error && (
          <div
            style={{
              padding: 10,
              border: '1px solid #cc2222',
              background: '#3a0a0a',
              color: '#ffaaaa',
              borderRadius: 4,
              marginBottom: 14,
              fontSize: 12,
            }}
          >
            {status.error}
          </div>
        )}

        {error && (
          <div
            style={{
              padding: 10,
              border: '1px solid #cc2222',
              background: '#3a0a0a',
              color: '#ffaaaa',
              borderRadius: 4,
              marginBottom: 14,
              fontSize: 12,
            }}
          >
            {error}
          </div>
        )}

        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button
            type="button"
            onClick={onClose}
            style={buttonStyle}
          >
            Hide overlay
          </button>
          <button
            type="button"
            onClick={onStop}
            disabled={busy}
            style={{
              ...buttonStyle,
              background: '#aa1111',
              borderColor: '#ff4444',
              color: '#fff',
              fontWeight: 700,
              minWidth: 120,
            }}
          >
            {busy ? 'Stopping…' : 'STOP'}
          </button>
        </div>
      </div>
    </div>
  )
}
